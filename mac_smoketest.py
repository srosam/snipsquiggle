"""
macOS smoke test for SnipSquiggle's platform-specific paths.

Run on the Mac (in Terminal), from the repo folder:

    python3 -m pip install -r requirements.txt
    python3 mac_smoketest.py

It checks, and prints PASS/FAIL for, each of:
  1. Platform + PyObjC availability
  2. Color-emoji rasterisation (Apple Color Emoji, incl. the tricky arrow glyph)
  3. Animated-GIF build (the shared engine)
  4. Clipboard: write the GIF via NSPasteboard, then read it back
  5. (optional) Non-interactive screen capture -> checks Screen Recording perm
  6. (optional) Interactive snip via the real capture path

Nothing here opens the full GUI.
"""

import os
import sys
import tempfile
import subprocess

RESULTS = []


def check(name, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))
    RESULTS.append(ok)
    return ok


def main():
    print("=== SnipSquiggle macOS smoke test ===\n")

    # 1) platform + pyobjc
    check("running on macOS", sys.platform == "darwin", f"sys.platform={sys.platform}")
    try:
        from AppKit import NSPasteboard, NSPasteboardItem  # noqa: F401
        from Foundation import NSData, NSURL  # noqa: F401
        check("PyObjC (AppKit/Foundation) importable", True)
    except Exception as e:
        check("PyObjC (AppKit/Foundation) importable", False, str(e))
        print("\n  Install with:  python3 -m pip install pyobjc-framework-Cocoa\n")

    # import the app engine
    try:
        import snipsquiggle as ss
        check("import snipsquiggle", True, f"emoji font: {ss._resolve_emoji_font() or '(none found)'}")
    except Exception as e:
        check("import snipsquiggle", False, str(e))
        _summary()
        return

    from PIL import Image

    # 2) emoji rasterisation (arrow + heart are the asymmetric ones)
    try:
        problems = []
        for ch in ["\U0001F525", "❤️", "➡️", "\U0001F44D"]:
            im = ss.emoji_image(ch, 40)
            colored = any(px[:3] != (px[0],) * 3 for px in im.convert("RGBA").getdata()
                          if px[3] > 0)
            if im.width < 8 or im.height < 8 or not colored:
                problems.append((ch, im.size, colored))
        check("color emoji render (fire/heart/arrow/thumb)", not problems,
              "all colored & sized" if not problems else f"issues: {problems}")
    except Exception as e:
        check("color emoji render", False, str(e))

    # 3) animated GIF via the shared engine
    gif_path = os.path.join(tempfile.gettempdir(), "SnipSquiggle_smoke.gif")
    static = None
    try:
        img = Image.new("RGB", (240, 130), (40, 110, 190))
        ed = ss.Editor.__new__(ss.Editor)  # bypass GUI
        ed.image = img
        pls = [ss.resample([(15, 105), (75, 30), (150, 100), (220, 35)])]
        ed.strokes = [{"ops": ss.build_ops(pls, "#ffcc00", 6, "emoji", "\U0001F525", 1)}]
        static = ss.Editor._write_gif(ed, gif_path)
        g = Image.open(gif_path)
        check("build animated GIF", getattr(g, "n_frames", 1) == ss.N_FRAMES,
              f"{g.n_frames} frames, {os.path.getsize(gif_path)} bytes -> {gif_path}")
    except Exception as e:
        check("build animated GIF", False, str(e))

    # 4) clipboard round-trip via NSPasteboard
    try:
        ss.set_clipboard(gif_path, static)
        from AppKit import NSPasteboard
        types = list(NSPasteboard.generalPasteboard().types())
        has_gif = any("gif" in str(t).lower() for t in types)
        check("clipboard: GIF present on NSPasteboard", has_gif, f"types={types}")
    except Exception as e:
        check("clipboard write/read", False, str(e))

    # 5) optional: non-interactive capture (verifies Screen Recording permission)
    if _ask("\nRun a non-interactive full-screen capture test? [y/N] "):
        try:
            p = os.path.join(tempfile.gettempdir(), "SnipSquiggle_fullshot.png")
            subprocess.run(["screencapture", "-x", p], check=False)
            ok = os.path.exists(p) and os.path.getsize(p) > 0
            dims = Image.open(p).size if ok else None
            check("screencapture full-screen", ok, f"{dims} -> {p}")
            print("      (If windows look missing/black, grant Terminal the "
                  "Screen Recording permission and re-run.)")
        except Exception as e:
            check("screencapture full-screen", False, str(e))

    # 6) optional: the real interactive snip path
    if _ask("\nRun the INTERACTIVE snip test (drag to select a region)? [y/N] "):
        try:
            print("  -> drag to select a region, or press Esc to cancel...")
            img = ss.mac_screencapture()
            if img is None:
                check("interactive snip", False, "cancelled or no file produced")
            else:
                out = os.path.join(tempfile.gettempdir(), "SnipSquiggle_snip.png")
                img.save(out)
                check("interactive snip", True, f"got {img.size} -> {out}")
        except Exception as e:
            check("interactive snip", False, str(e))

    _summary()


def _ask(prompt):
    try:
        return input(prompt).strip().lower().startswith("y")
    except EOFError:
        return False


def _summary():
    print("\n=== summary ===")
    total, passed = len(RESULTS), sum(RESULTS)
    print(f"{passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
