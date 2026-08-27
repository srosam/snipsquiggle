"""
SnipSquiggle - a Snipping-Tool-style capture + animated annotation app.

Cross-platform (Windows fully tested; macOS/Linux paths included).

Run modes:
  Default           One-shot: launch -> snip -> edit -> exit.
  --tray            Resident: stays in the tray / menu bar and snips on a
                    global hotkey. Windows: system tray, PrintScreen.
                    macOS: menu-bar icon, Cmd+Shift+2 (macOS has no PrintScreen
                    and reserves Cmd+Shift+3/4/5). The icon's menu snips or
                    quits; closing the editor returns to idle instead of exiting.
                    The hotkey also works while the editor is open — it replaces
                    the current snip with a new one.
                    (Linux tray/hotkey not implemented — falls back to one-shot.)
  --no-copy         Don't put the plain snip on the clipboard automatically.

Flow:
  1. Launch (or press the hotkey in --tray mode) -> select a region to snip.
       Windows/Linux: a dimmed overlay, drag a rectangle (Esc cancels).
       macOS:         the native `screencapture -i` crosshair.
  1b. The plain snip lands on the clipboard immediately as a static image, so
      you can paste right away without annotating (--no-copy disables this).
  2. Editor opens with your snip. Draw with pen / arrow / box, and pick an
     animation style per stroke:
        Boil  - hand-drawn squiggle that gently wobbles (default)
        Ants  - marching-ants moving dashes
        Dots  - dots flowing along the stroke
        Emoji - emojis marching along the stroke (🔥 ❤️ ⭐ ...)
     Optionally add a company/logo watermark (💧 Logo): drag to reposition,
     mouse-wheel over it to resize. It ripples with a gentle water wibble.
  3. Ctrl+C copies a looping animated GIF to the clipboard.
       Windows: CF_HDROP (file) + CF_DIB (static) + "GIF" bytes
       macOS:   NSPasteboard public.gif + public.png + file URL
       Linux:   xclip / wl-copy image/gif (best effort)

Deps: pillow (all), pywin32 (Windows), pyobjc-framework-Cocoa (macOS).
See requirements.txt.
"""

import io
import os
import sys
import json
import math
import queue
import bisect
import random
import tempfile
import threading
import subprocess
import ctypes

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
from PIL import Image, ImageTk, ImageGrab, ImageDraw, ImageFont, ImageOps

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WIN and not IS_MAC

# ---------------------------------------------------------------------------
# DPI awareness (Windows) so tkinter pixel coords match the physical screenshot.
# ---------------------------------------------------------------------------
if IS_WIN:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Animation / drawing tuning
# ---------------------------------------------------------------------------
N_FRAMES = 8          # frames in the loop (more = smoother marching, bigger gif)
FRAME_MS = 90         # on-screen speed and gif frame delay (ms)
RESAMPLE_SPACING = 7  # px between points before wobble
JITTER_BASE = 1.8     # base wobble amplitude (px)

PALETTE = ["#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#000000", "#ffffff"]
EMOJIS = ["🔥", "❤️", "⭐", "✅", "👍", "😂", "🎉", "➡️", "💯", "👀", "😭", "🤬", "FFS", "💤"]

# tk.Button ignores bg/fg on macOS (native Aqua button), so we build toolbar
# controls from Labels, which honor colors on every platform.
UI_FONT = ("Segoe UI", 9) if IS_WIN else ("Helvetica", 12)
EMOJI_UI_FONT = ("Segoe UI Emoji", 12) if IS_WIN else ("Helvetica", 15)
BTN_BG = "#2d2d2d"
BTN_SEL = "#0a84ff"


def _lighten(hexstr, amt=0.16):
    hexstr = hexstr.lstrip("#")
    r, g, b = (int(hexstr[i:i + 2], 16) for i in (0, 2, 4))
    r = int(r + (255 - r) * amt)
    g = int(g + (255 - g) * amt)
    b = int(b + (255 - b) * amt)
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def resample(points, spacing=RESAMPLE_SPACING):
    """Evenly space a polyline by arc length so patterns look uniform."""
    pts = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    if len(pts) < 2:
        return pts
    out = [pts[0]]
    prev = pts[0]
    acc = 0.0
    for p in pts[1:]:
        d = _dist(prev, p)
        if d == 0:
            continue
        while acc + d >= spacing:
            t = (spacing - acc) / d
            nx = prev[0] + t * (p[0] - prev[0])
            ny = prev[1] + t * (p[1] - prev[1])
            out.append((nx, ny))
            prev = (nx, ny)
            d = _dist(prev, p)
            acc = 0.0
        acc += d
        prev = p
    if _dist(out[-1], pts[-1]) > 0.5:
        out.append(pts[-1])
    return out


def rect_polyline(p0, p1):
    x0, y0 = p0
    x1, y1 = p1
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    out = []
    for a, b in zip(corners, corners[1:]):
        seg = resample([a, b])
        out.extend(seg if not out else seg[1:])
    return out


def arrow_polylines(p0, p1, width):
    shaft = resample([p0, p1])
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    head = max(12, width * 4)
    spread = math.radians(28)
    b1 = (p1[0] - head * math.cos(ang - spread), p1[1] - head * math.sin(ang - spread))
    b2 = (p1[0] - head * math.cos(ang + spread), p1[1] - head * math.sin(ang + spread))
    return [shaft, resample([p1, b1]), resample([p1, b2])]


def cumlen(pts):
    cl = [0.0]
    for a, b in zip(pts, pts[1:]):
        cl.append(cl[-1] + _dist(a, b))
    return cl


def point_at(pts, cl, s):
    """Point at arc-length s along a polyline."""
    if s <= 0:
        return pts[0]
    if s >= cl[-1]:
        return pts[-1]
    i = bisect.bisect_right(cl, s) - 1
    seg = cl[i + 1] - cl[i]
    t = 0.0 if seg == 0 else (s - cl[i]) / seg
    ax, ay = pts[i]
    bx, by = pts[i + 1]
    return (ax + (bx - ax) * t, ay + (by - ay) * t)


def squiggle_variants(polylines, width, n, seed):
    """Smoothly-looping wobble: each point sways on a sine so frame 0 == frame N."""
    amp = JITTER_BASE + width * 0.35
    rnd = random.Random(seed)
    meta = [[(rnd.uniform(0, 2 * math.pi), rnd.uniform(0, 2 * math.pi))
             for _ in pl] for pl in polylines]
    frames = []
    for f in range(n):
        a = 2 * math.pi * f / max(1, n)
        frame = []
        for pl, plm in zip(polylines, meta):
            jl = []
            last = len(pl) - 1
            for i, ((x, y), (phase, theta)) in enumerate(zip(pl, plm)):
                edge = 0.4 if (i == 0 or i == last) else 1.0
                o = amp * edge * math.sin(a + phase)
                jl.append((x + math.cos(theta) * o, y + math.sin(theta) * o))
            frame.append(jl)
        frames.append(frame)
    return frames


def wobble_once(polylines, width, seed):
    return squiggle_variants(polylines, width, 1, seed)[0]


def dash_segments(pts, cl, phase, on, off):
    """Return list of point-runs that are 'on' for a marching-dash phase."""
    period = on + off
    L = cl[-1]
    segs, cur = [], None
    s = 0.0
    while s <= L:
        p = point_at(pts, cl, s)
        if ((s - phase) % period) < on:
            (cur := cur or []).append(p)
        else:
            if cur and len(cur) >= 2:
                segs.append(cur)
            cur = None
        s += 2.0
    if cur and len(cur) >= 2:
        segs.append(cur)
    return segs


def spaced_positions(pts, cl, phase, spacing):
    """Positions every `spacing` px along the path, marching forward with phase
    (same direction convention as the stroke was drawn / marching ants)."""
    L = cl[-1]
    out = []
    s = phase % spacing
    while s <= L:
        out.append(point_at(pts, cl, s))
        s += spacing
    if not out:
        out.append(point_at(pts, cl, L / 2))
    return out


# ---------------------------------------------------------------------------
# Emoji rasterisation (color glyphs), cross-platform + cached.
# Windows: Segoe UI Emoji (scalable COLR). macOS: Apple Color Emoji (fixed
# bitmap strikes -> render at a strike size then downscale). Linux: Noto Color
# Emoji if installed. Drop a font in ./assets to override on any platform.
# ---------------------------------------------------------------------------
_FONT_CACHE = {}
_EMOJI_CACHE = {}
_EMOJI_FONT_PATH = None


def _emoji_font_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    cands = []
    assets = os.path.join(here, "assets")
    if os.path.isdir(assets):
        for n in ("NotoColorEmoji.ttf", "emoji.ttf", "emoji.ttc",
                  "seguiemj.ttf", "Apple Color Emoji.ttc"):
            cands.append(os.path.join(assets, n))
    if IS_WIN:
        cands.append(r"C:\Windows\Fonts\seguiemj.ttf")
    elif IS_MAC:
        cands.append("/System/Library/Fonts/Apple Color Emoji.ttc")
    else:
        cands += [
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
            "/usr/share/fonts/NotoColorEmoji.ttf",
        ]
    return [p for p in cands if os.path.exists(p)]


def _resolve_emoji_font():
    global _EMOJI_FONT_PATH
    if _EMOJI_FONT_PATH is None:
        paths = _emoji_font_candidates()
        _EMOJI_FONT_PATH = paths[0] if paths else ""
    return _EMOJI_FONT_PATH


def _emoji_font(path, size):
    key = (path, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(path, size)
    return _FONT_CACHE[key]


def _px_order(px):
    """Sizes to try: requested first (works for scalable fonts), then the common
    color-bitmap strike sizes (Apple 160/128/96/64..., Noto 136/109)."""
    order, seen, out = [px, 160, 137, 136, 128, 109, 96, 64, 48, 40, 32, 20], set(), []
    for s in order:
        s = int(s)
        if s > 0 and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def emoji_image(char, px):
    key = (char, px)
    if key in _EMOJI_CACHE:
        return _EMOJI_CACHE[key]
    path = _resolve_emoji_font()
    glyph = None
    if path:
        for rpx in _px_order(px):
            try:
                font = _emoji_font(path, rpx)
                # Oversized canvas so asymmetric glyphs (arrow/heart) never clip.
                box = int(rpx * 3)
                tmp = Image.new("RGBA", (box, box), (0, 0, 0, 0))
                d = ImageDraw.Draw(tmp)
                d.text((box / 2, box / 2), char, font=font,
                       anchor="mm", embedded_color=True)
                bb = tmp.getbbox()
                if not bb:
                    continue
                g = tmp.crop(bb)
                if rpx != px:
                    fac = px / rpx
                    g = g.resize((max(1, round(g.width * fac)),
                                  max(1, round(g.height * fac))), Image.LANCZOS)
                glyph = g
                break
            except Exception:
                continue
    if glyph is None:  # last-ditch: monochrome text
        box = int(px * 1.4)
        glyph = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        try:
            ImageDraw.Draw(glyph).text((box / 2, box / 2), char, anchor="mm",
                                       fill=(0, 0, 0, 255))
        except Exception:
            pass
    _EMOJI_CACHE[key] = glyph
    return glyph


# ---------------------------------------------------------------------------
# Watermark / logo ripple. Warp an RGBA logo with a gentle, smoothly-looping
# water "wibble" so frame 0 == frame N (seamless GIF loop). Implemented as a
# per-frame PIL MESH transform: the image is diced into a grid and each cell's
# source quad is nudged by two orthogonal sines whose phase advances with the
# frame, giving a shimmering ripple.
# ---------------------------------------------------------------------------
def ripple_variants(logo, n, strength=0.018):
    """Return n RGBA frames of `logo`, each rippled, looping over the phase.

    strength is the wobble amplitude as a fraction of the logo's smaller side.
    A transparent border is added so displaced samples never clip the edges.
    """
    logo = logo.convert("RGBA")
    amp = max(1.5, min(logo.size) * strength)
    pad = int(math.ceil(amp)) + 2
    img = ImageOps.expand(logo, border=pad, fill=(0, 0, 0, 0))
    w, h = img.size

    # ~1.3 waves across the width, ~1.7 down the height -> organic, not gridded.
    kx = 2 * math.pi * 1.3 / max(1, w)
    ky = 2 * math.pi * 1.7 / max(1, h)
    step = max(6, min(w, h) // 16)
    xs = list(range(0, w, step)) + [w]
    ys = list(range(0, h, step)) + [h]

    frames = []
    for f in range(n):
        ph = 2 * math.pi * f / max(1, n)

        def src(x, y):
            return (x + amp * math.sin(y * ky + ph),
                    y + amp * math.cos(x * kx + ph))

        mesh = []
        for iy in range(len(ys) - 1):
            for ix in range(len(xs) - 1):
                x1, x2 = xs[ix], xs[ix + 1]
                y1, y2 = ys[iy], ys[iy + 1]
                nw, sw = src(x1, y1), src(x1, y2)
                se, ne = src(x2, y2), src(x2, y1)
                # MESH src quad order: NW, SW, SE, NE
                quad = (nw[0], nw[1], sw[0], sw[1],
                        se[0], se[1], ne[0], ne[1])
                mesh.append(((x1, y1, x2, y2), quad))
        frames.append(img.transform((w, h), Image.MESH, mesh, Image.BILINEAR))
    return frames


# ---------------------------------------------------------------------------
# Recently-used logos: a small JSON list of absolute paths (most-recent first),
# stored per-user so the picker remembers logos across sessions.
# ---------------------------------------------------------------------------
RECENT_PATH = os.path.join(os.path.expanduser("~"), ".snipsquiggle_recent.json")
MAX_RECENT = 8


def load_recent_logos():
    try:
        with open(RECENT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, str)]
    except Exception:
        return []


def add_recent_logo(path):
    path = os.path.abspath(path)
    same = os.path.normcase(path)
    recent = [p for p in load_recent_logos() if os.path.normcase(p) != same]
    recent.insert(0, path)
    try:
        with open(RECENT_PATH, "w", encoding="utf-8") as f:
            json.dump(recent[:MAX_RECENT], f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Build per-frame draw "ops" for a stroke. Ops are backend-agnostic:
#   ("line", [pts], color, width)
#   ("dot",  x, y, r, color)
#   ("emoji", x, y, char, px)
# ---------------------------------------------------------------------------
def build_ops(polylines, color, width, anim, emoji, seed):
    frames = []

    if anim == "squiggle":
        variants = squiggle_variants(polylines, width, N_FRAMES, seed)
        for f in range(N_FRAMES):
            frames.append([("line", pl, color, width)
                           for pl in variants[f] if len(pl) >= 2])
        return frames

    base = wobble_once(polylines, width, seed)
    metas = [(pl, cumlen(pl)) for pl in base if len(pl) >= 2]

    if anim == "ants":
        on = max(6, width * 2.2)
        off = max(6, width * 2.2)
        period = on + off
        for f in range(N_FRAMES):
            phase = period * f / N_FRAMES
            ops = []
            for pl, cl in metas:
                for seg in dash_segments(pl, cl, phase, on, off):
                    ops.append(("line", seg, color, width))
            frames.append(ops)

    elif anim == "dots":
        spacing = max(12, width * 3.0)
        r = max(2.5, width * 0.95)
        for f in range(N_FRAMES):
            phase = spacing * f / N_FRAMES
            ops = []
            for pl, cl in metas:
                for (x, y) in spaced_positions(pl, cl, phase, spacing):
                    ops.append(("dot", x, y, r, color))
            frames.append(ops)

    elif anim == "emoji":
        px = int(max(20, width * 5))
        spacing = px * 1.15
        for f in range(N_FRAMES):
            phase = spacing * f / N_FRAMES
            ops = []
            for pl, cl in metas:
                for i, (x, y) in enumerate(spaced_positions(pl, cl, phase, spacing)):
                    bob = math.sin(2 * math.pi * f / N_FRAMES + i) * px * 0.08
                    ops.append(("emoji", x, y + bob, emoji, px))
            frames.append(ops)

    else:
        for _ in range(N_FRAMES):
            frames.append([("line", pl, color, width) for pl, _cl in metas])

    return frames


# ---------------------------------------------------------------------------
# Clipboard: put a looping animated GIF on the system clipboard.
# ---------------------------------------------------------------------------
def set_clipboard(gif_path, static_frame):
    if IS_WIN:
        _clip_win(gif_path, static_frame)
    elif IS_MAC:
        _clip_mac(gif_path, static_frame)
    else:
        _clip_linux(gif_path, static_frame)


def set_clipboard_image(image):
    """Put a plain static image on the clipboard (no GIF, no file reference).

    Used right after a snip so the raw screenshot is pasteable immediately.
    """
    if IS_WIN:
        _clip_image_win(image)
    elif IS_MAC:
        _clip_image_mac(image)
    else:
        _clip_image_linux(image)


def _png_bytes(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


if IS_WIN:
    from ctypes import wintypes

    _k32 = ctypes.windll.kernel32
    _u32 = ctypes.windll.user32
    _k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _k32.GlobalAlloc.restype = ctypes.c_void_p
    _k32.GlobalLock.argtypes = [ctypes.c_void_p]
    _k32.GlobalLock.restype = ctypes.c_void_p
    _k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _k32.GlobalUnlock.restype = wintypes.BOOL
    _u32.OpenClipboard.argtypes = [wintypes.HWND]
    _u32.OpenClipboard.restype = wintypes.BOOL
    _u32.EmptyClipboard.restype = wintypes.BOOL
    _u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    _u32.SetClipboardData.restype = ctypes.c_void_p
    _u32.CloseClipboard.restype = wintypes.BOOL
    _u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    _u32.RegisterClipboardFormatW.restype = wintypes.UINT

    _GMEM_MOVEABLE = 0x0002
    _CF_DIB = 8
    _CF_HDROP = 15

    def _global_from_bytes(data):
        h = _k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        ptr = _k32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        _k32.GlobalUnlock(h)
        return h

    def _hdrop_bytes(paths):
        import struct
        header = struct.pack("<IiiiI", 20, 0, 0, 0, 1)  # DROPFILES, fWide=1
        body = "".join(p + "\0" for p in paths) + "\0"
        return header + body.encode("utf-16-le")

    def _dib_bytes(im):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "BMP")
        return buf.getvalue()[14:]  # strip BITMAPFILEHEADER -> CF_DIB

    def _put_formats(payloads):
        if not _u32.OpenClipboard(None):
            raise OSError("Could not open clipboard")
        try:
            _u32.EmptyClipboard()
            for fmt, data in payloads.items():
                _u32.SetClipboardData(fmt, _global_from_bytes(data))
        finally:
            _u32.CloseClipboard()

    def _clip_win(gif_path, static_frame):
        cf_gif = _u32.RegisterClipboardFormatW("GIF")
        with open(gif_path, "rb") as f:
            gif_bytes = f.read()
        _put_formats({
            _CF_HDROP: _hdrop_bytes([gif_path]),
            _CF_DIB: _dib_bytes(static_frame),
            cf_gif: gif_bytes,
        })

    def _clip_image_win(im):
        # CF_DIB is the universal bitmap format; "PNG" is what browsers and
        # newer editors reach for first.
        cf_png = _u32.RegisterClipboardFormatW("PNG")
        _put_formats({_CF_DIB: _dib_bytes(im), cf_png: _png_bytes(im)})


def _clip_mac(gif_path, static_frame):
    # PyObjC: set gif data, png fallback, and a file URL (for file-paste targets).
    from AppKit import NSPasteboard, NSPasteboardItem
    from Foundation import NSData, NSURL

    with open(gif_path, "rb") as f:
        gif = f.read()
    png_buf = io.BytesIO()
    static_frame.convert("RGB").save(png_buf, "PNG")
    png = png_buf.getvalue()

    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    item = NSPasteboardItem.alloc().init()
    item.setData_forType_(NSData.dataWithBytes_length_(gif, len(gif)),
                          "com.compuserve.gif")
    item.setData_forType_(NSData.dataWithBytes_length_(png, len(png)),
                          "public.png")
    url = NSURL.fileURLWithPath_(gif_path)
    if not pb.writeObjects_([item, url]):
        raise OSError("NSPasteboard writeObjects failed")


def _clip_image_mac(im):
    from AppKit import NSPasteboard, NSPasteboardItem
    from Foundation import NSData

    png = _png_bytes(im)
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    item = NSPasteboardItem.alloc().init()
    item.setData_forType_(NSData.dataWithBytes_length_(png, len(png)),
                          "public.png")
    if not pb.writeObjects_([item]):
        raise OSError("NSPasteboard writeObjects failed")


def _clip_linux(gif_path, static_frame):
    import shutil
    with open(gif_path, "rb") as f:
        gif = f.read()
    if shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/gif"],
                       input=gif, check=True)
        return
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy", "--type", "image/gif"], input=gif, check=True)
        return
    raise OSError("No clipboard tool found. Install 'xclip' (X11) or "
                  "'wl-clipboard' (Wayland). The GIF was still saved.")


def _clip_image_linux(im):
    import shutil
    png = _png_bytes(im)
    if shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png"],
                       input=png, check=True)
        return
    if shutil.which("wl-copy"):
        subprocess.run(["wl-copy", "--type", "image/png"], input=png, check=True)
        return
    raise OSError("No clipboard tool found. Install 'xclip' (X11) or "
                  "'wl-clipboard' (Wayland).")


# ===========================================================================
# Screen capture
# ===========================================================================
def mac_screencapture():
    """Native macOS interactive snip. Returns a PIL.Image or None if cancelled."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        os.remove(path)  # screencapture only writes the file if a snip is made
    except OSError:
        pass
    try:
        subprocess.run(["screencapture", "-i", "-x", path], check=False)
    except FileNotFoundError:
        return None
    if os.path.exists(path) and os.path.getsize(path) > 0:
        img = Image.open(path).convert("RGB")
        img.load()
        try:
            os.remove(path)
        except OSError:
            pass
        return img
    return None


class OverlayCapture:
    """Dimmed full-screen overlay with drag-to-select (Windows / Linux)."""

    def __init__(self, root, on_done):
        self.root = root
        self.on_done = on_done

        self.shot = ImageGrab.grab(all_screens=True) if IS_WIN else ImageGrab.grab()
        if IS_WIN:
            vx = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            vy = ctypes.windll.user32.GetSystemMetrics(77)
        else:
            vx = vy = 0
        vw, vh = self.shot.size

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.geometry(f"{vw}x{vh}+{vx}+{vy}")
        self.win.attributes("-topmost", True)
        self.win.config(cursor="crosshair")

        self.canvas = tk.Canvas(self.win, width=vw, height=vh,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self.tkimg = ImageTk.PhotoImage(self.shot)
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg)

        self.dim = [self.canvas.create_rectangle(0, 0, vw, vh, fill="black",
                    stipple="gray50", outline="") for _ in range(4)]
        self.sel = self.canvas.create_rectangle(0, 0, 0, 0, outline="#0a84ff",
                                                width=2)
        self.hint = self.canvas.create_text(vw // 2, 30,
                    text="Drag to snip   •   Esc to cancel",
                    fill="white", font=("Segoe UI", 14, "bold"))

        self.start = None
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)
        self.win.focus_force()

    def _down(self, e):
        self.start = (e.x, e.y)
        self.canvas.itemconfig(self.hint, state="hidden")

    def _move(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        lx, ty = min(x0, e.x), min(y0, e.y)
        rx, by = max(x0, e.x), max(y0, e.y)
        self.canvas.coords(self.sel, lx, ty, rx, by)
        W, H = self.shot.size
        self.canvas.coords(self.dim[0], 0, 0, W, ty)
        self.canvas.coords(self.dim[1], 0, by, W, H)
        self.canvas.coords(self.dim[2], 0, ty, lx, by)
        self.canvas.coords(self.dim[3], rx, ty, W, by)

    def _up(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        lx, ty = min(x0, e.x), min(y0, e.y)
        rx, by = max(x0, e.x), max(y0, e.y)
        self.win.destroy()
        if rx - lx < 5 or by - ty < 5:
            self.on_done(None)
            return
        self.on_done(self.shot.crop((lx, ty, rx, by)))

    def _cancel(self):
        self.win.destroy()
        self.on_done(None)


# ===========================================================================
# Editor
# ===========================================================================
class Editor:
    def __init__(self, root, image, new_snip_cb, on_close=None,
                 static_copied=False):
        self.root = root
        self.image = image.convert("RGB")
        self.new_snip_cb = new_snip_cb
        self.on_close = on_close or root.quit

        self.color = "#ff3b30"
        self.width = 5
        self.tool = "pen"          # pen | arrow | box
        self.anim = "squiggle"     # squiggle | ants | dots | emoji
        self.emoji = "🔥"
        self.strokes = []
        self.frame = 0
        self._seed = 0
        self._live_pts = []
        self._live_start = None
        self._emoji_photos = {}
        self.watermark = None      # see load_watermark() for shape
        self._wm_drag = None       # (dx, dy) offset while dragging the logo
        self._closed = False
        self._tick_id = None

        self.win = tk.Toplevel(root)
        self.win.title("SnipSquiggle")
        self.win.configure(bg="#1e1e1e")
        self.win.protocol("WM_DELETE_WINDOW", self._quit)

        self._build_toolbar()
        if static_copied:
            self._toast("📋 Screenshot copied — paste it, or annotate below")

        self.tkimg = ImageTk.PhotoImage(self.image)
        self.canvas = tk.Canvas(self.win, width=self.image.width,
                                height=self.image.height,
                                highlightthickness=0, bd=0, cursor="pencil")
        self.canvas.pack(padx=10, pady=(0, 10))
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg, tags="bg")

        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)

        # Mouse wheel resizes the watermark when hovering over it.
        self.canvas.bind("<MouseWheel>", self._on_wheel)                 # Win/Mac
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))   # Linux
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))  # Linux

        # Ctrl on Win/Linux, Command on macOS
        mod = "Command" if IS_MAC else "Control"
        self.win.bind(f"<{mod}-c>", lambda e: self.copy_gif())
        self.win.bind(f"<{mod}-s>", lambda e: self.save_gif())
        self.win.bind(f"<{mod}-z>", lambda e: self.undo())
        self.win.bind(f"<{mod}-n>", lambda e: self.new_snip())
        self.win.bind("<Escape>", lambda e: self._quit())

        # PrintScreen while the editor is focused starts a fresh snip. In tray
        # mode the global hotkey claims the key before it reaches us and does
        # the same thing; this covers one-shot mode (and a failed registration).
        # Windows only delivers PrintScreen on key *release*, so bind both.
        for seq in ("<Key-Print>", "<KeyRelease-Print>"):
            self.win.bind(seq, lambda e: self.new_snip())

        self.win.after(200, self.win.focus_force)
        self._tick()

    # -- toolbar ------------------------------------------------------------
    def _btn(self, parent, text, cmd, base=BTN_BG, fg="white", font=None,
             padx=10, pady=4):
        # A Label styled as a button (colors work on macOS; tk.Button doesn't).
        b = tk.Label(parent, text=text, bg=base, fg=fg, padx=padx, pady=pady,
                     font=font or UI_FONT, cursor="hand2")
        b._basebg = base
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=_lighten(b._basebg)))
        b.bind("<Leave>", lambda e: b.configure(bg=b._basebg))
        b.pack(side="left", padx=2)
        return b

    @staticmethod
    def _set_sel(widget, selected):
        widget._basebg = BTN_SEL if selected else BTN_BG
        widget.configure(bg=widget._basebg)

    def _sep(self, parent):
        tk.Frame(parent, width=1, bg="#444").pack(side="left", fill="y", padx=6)

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg="#1e1e1e", fg="#9a9a9a",
                 font=("Segoe UI", 9)).pack(side="left", padx=(4, 2))

    def _build_toolbar(self):
        mod = "Cmd" if IS_MAC else "Ctrl"
        bar = tk.Frame(self.win, bg="#1e1e1e")
        bar.pack(fill="x", padx=10, pady=(8, 2))

        self.tool_btns = {}
        for name, label in (("pen", "✎ Pen"), ("arrow", "↗ Arrow"), ("box", "▭ Box")):
            self.tool_btns[name] = self._btn(bar, label, lambda n=name: self.set_tool(n))
        self._sep(bar)
        self.swatches = {}
        for c in PALETTE:
            sw = tk.Frame(bar, bg=c, width=22, height=22, cursor="hand2",
                          highlightbackground="#555", highlightthickness=1)
            sw.pack_propagate(False)
            sw.bind("<Button-1>", lambda e, cc=c: self.set_color(cc))
            sw.pack(side="left", padx=1)
            self.swatches[c] = sw
        self._btn(bar, "＋", self.pick_color)
        self._sep(bar)
        for w, lbl in ((3, "S"), (5, "M"), (9, "L"), (14, "XL")):
            self._btn(bar, lbl, lambda ww=w: self.set_width(ww))
        self._sep(bar)
        self._btn(bar, "↶ Undo", self.undo)
        self._btn(bar, "✕ Clear", self.clear)
        self._sep(bar)
        self.logo_btn = self._btn(bar, "💧 Logo", self.load_watermark)
        self.logo_rm_btn = self._btn(bar, "🚫", self.remove_watermark)
        self._label(bar, "drag · scroll to size")

        right = tk.Frame(bar, bg="#1e1e1e")
        right.pack(side="right")
        self._btn(right, f"＋ New ({mod}+N)", self.new_snip)
        self._btn(right, f"💾 Save ({mod}+S)", self.save_gif)
        self.copy_btn = self._btn(right, f"📋 Copy GIF ({mod}+C)", self.copy_gif,
                                  base=BTN_SEL, padx=12,
                                  font=(UI_FONT[0], UI_FONT[1], "bold"))

        bar2 = tk.Frame(self.win, bg="#1e1e1e")
        bar2.pack(fill="x", padx=10, pady=(0, 6))
        self._label(bar2, "Animation:")
        self.anim_btns = {}
        for name, label in (("squiggle", "〰 Boil"), ("ants", "┅ Ants"),
                            ("dots", "•• Dots"), ("emoji", "😀 Emoji")):
            self.anim_btns[name] = self._btn(bar2, label, lambda n=name: self.set_anim(n))
        self._sep(bar2)
        self.emoji_btns = {}
        for ch in EMOJIS:
            self.emoji_btns[ch] = self._btn(bar2, ch,
                                            lambda c=ch: self.set_emoji(c),
                                            font=EMOJI_UI_FONT, padx=5, pady=2)

        self.status = tk.Label(bar2, text="", bg="#1e1e1e", fg="#4cd964",
                               font=UI_FONT)
        self.status.pack(side="right", padx=(6, 2))

        self._refresh_btns()

    def _refresh_btns(self):
        for name, b in self.tool_btns.items():
            self._set_sel(b, name == self.tool)
        for name, b in self.anim_btns.items():
            self._set_sel(b, name == self.anim)
        for ch, b in self.emoji_btns.items():
            self._set_sel(b, self.anim == "emoji" and ch == self.emoji)
        for c, sw in getattr(self, "swatches", {}).items():
            sel = (c == self.color)
            sw.configure(highlightbackground="#ffffff" if sel else "#555",
                         highlightthickness=2 if sel else 1)

    def set_tool(self, name):
        self.tool = name
        self._refresh_btns()

    def set_anim(self, name):
        self.anim = name
        self._refresh_btns()

    def set_emoji(self, ch):
        self.emoji = ch
        self.anim = "emoji"
        self._refresh_btns()

    def set_color(self, c):
        self.color = c
        self._refresh_btns()

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color, parent=self.win)[1]
        if c:
            self.color = c
            self._refresh_btns()

    def set_width(self, w):
        self.width = w

    # -- watermark / logo ---------------------------------------------------
    def load_watermark(self):
        """Pop a menu of recently-used logos + Browse…; browse directly if none."""
        recent = [p for p in load_recent_logos() if os.path.exists(p)]
        if not recent:
            self._browse_watermark()
            return
        menu = tk.Menu(self.win, tearoff=0)
        for p in recent:
            menu.add_command(label=os.path.basename(p),
                             command=lambda pp=p: self._use_watermark(pp))
        menu.add_separator()
        menu.add_command(label="Browse…", command=self._browse_watermark)
        try:
            menu.tk_popup(self.win.winfo_pointerx(), self.win.winfo_pointery())
        finally:
            menu.grab_release()

    def _browse_watermark(self):
        path = filedialog.askopenfilename(
            parent=self.win, title="Choose a logo / watermark",
            filetypes=[("Images", "*.png *.gif *.jpg *.jpeg *.bmp *.webp"),
                       ("All files", "*.*")])
        if path:
            self._use_watermark(path)

    def _use_watermark(self, path):
        try:
            natural = Image.open(path).convert("RGBA")
            natural.load()
        except Exception as ex:
            messagebox.showerror("Load failed", str(ex), parent=self.win)
            return
        add_recent_logo(path)
        # Fit to ~22% of the snip width on first load.
        scale = min(1.0, (self.image.width * 0.22) / natural.width)
        self.watermark = {
            "natural": natural,
            "scale": scale,
            "cx": self.image.width - 1,   # placed properly by _rebuild_watermark
            "cy": self.image.height - 1,
            "place_corner": True,         # snap to bottom-right until first drag
        }
        self._rebuild_watermark()
        self._flash(self.logo_btn, "💧 drag · scroll")

    def remove_watermark(self):
        self.watermark = None
        self._wm_drag = None

    def _rebuild_watermark(self):
        """(Re)scale the logo and precompute its rippled frames + canvas photos."""
        wm = self.watermark
        if not wm:
            return
        nat = wm["natural"]
        bw = max(1, int(nat.width * wm["scale"]))
        bh = max(1, int(nat.height * wm["scale"]))
        base = nat.resize((bw, bh), Image.LANCZOS)
        frames = ripple_variants(base, N_FRAMES)
        wm["frames_img"] = frames
        wm["photos"] = [ImageTk.PhotoImage(fr) for fr in frames]
        wm["w"], wm["h"] = frames[0].size
        if wm.pop("place_corner", False):
            m = 14 + max(wm["w"], wm["h"]) / 2
            wm["cx"] = self.image.width - m
            wm["cy"] = self.image.height - m
        # Keep the logo on-canvas after a resize.
        wm["cx"] = min(max(wm["cx"], wm["w"] / 2), self.image.width - wm["w"] / 2)
        wm["cy"] = min(max(wm["cy"], wm["h"] / 2), self.image.height - wm["h"] / 2)

    def _in_watermark(self, x, y):
        wm = self.watermark
        if not wm:
            return False
        return (abs(x - wm["cx"]) <= wm["w"] / 2 and
                abs(y - wm["cy"]) <= wm["h"] / 2)

    def _on_wheel(self, e, direction=None):
        wm = self.watermark
        if not wm or not self._in_watermark(e.x, e.y):
            return
        if direction is None:                       # Win/Mac carry delta
            direction = 1 if getattr(e, "delta", 0) > 0 else -1
        wm["scale"] = max(0.05, min(8.0, wm["scale"] * (1.1 if direction > 0 else 0.9)))
        self._rebuild_watermark()

    # -- drawing ------------------------------------------------------------
    def _down(self, e):
        # Clicking on the logo grabs it for repositioning instead of drawing.
        if self._in_watermark(e.x, e.y):
            wm = self.watermark
            self._wm_drag = (e.x - wm["cx"], e.y - wm["cy"])
            return
        self._live_start = (e.x, e.y)
        self._live_pts = [(e.x, e.y)]

    def _move(self, e):
        if self._wm_drag is not None:
            wm = self.watermark
            dx, dy = self._wm_drag
            wm["cx"] = min(max(e.x - dx, wm["w"] / 2), self.image.width - wm["w"] / 2)
            wm["cy"] = min(max(e.y - dy, wm["h"] / 2), self.image.height - wm["h"] / 2)
            return
        if self._live_start is None:
            return
        p = (e.x, e.y)
        self._live_pts.append(p)
        self.canvas.delete("live")
        if self.tool == "pen":
            if len(self._live_pts) >= 2:
                self.canvas.create_line(*sum(self._live_pts, ()), fill=self.color,
                                        width=self.width, capstyle="round",
                                        joinstyle="round", smooth=True, tags="live")
        elif self.tool == "box":
            x0, y0 = self._live_start
            self.canvas.create_rectangle(x0, y0, p[0], p[1], outline=self.color,
                                         width=self.width, tags="live")
        else:
            x0, y0 = self._live_start
            self.canvas.create_line(x0, y0, p[0], p[1], fill=self.color,
                                    width=self.width, capstyle="round",
                                    arrow="last", tags="live")

    def _up(self, e):
        if self._wm_drag is not None:
            self._wm_drag = None
            return
        if self._live_start is None:
            return
        self.canvas.delete("live")
        start, end = self._live_start, (e.x, e.y)
        self._live_start = None

        if self.tool == "pen":
            if len(self._live_pts) < 2:
                return
            polylines = [resample(self._live_pts)]
        elif self.tool == "box":
            if _dist(start, end) < 5:
                return
            polylines = [rect_polyline(start, end)]
        else:
            if _dist(start, end) < 5:
                return
            polylines = arrow_polylines(start, end, self.width)

        self._seed += 1
        ops = build_ops(polylines, self.color, self.width,
                        self.anim, self.emoji, self._seed)
        self.strokes.append({"ops": ops})

    def undo(self):
        if self.strokes:
            self.strokes.pop()

    def clear(self):
        self.strokes.clear()

    # -- canvas animation loop ---------------------------------------------
    def _emoji_photo(self, char, px):
        key = (char, int(px))
        if key not in self._emoji_photos:
            self._emoji_photos[key] = ImageTk.PhotoImage(emoji_image(char, int(px)))
        return self._emoji_photos[key]

    def _tick(self):
        if self._closed:
            return
        self.frame = (self.frame + 1) % N_FRAMES
        self.canvas.delete("stroke")
        if self.watermark:
            wm = self.watermark
            self.canvas.create_image(wm["cx"], wm["cy"],
                                     image=wm["photos"][self.frame], tags="stroke")
        for s in self.strokes:
            for op in s["ops"][self.frame]:
                kind = op[0]
                if kind == "line":
                    _, pts, col, w = op
                    if len(pts) >= 2:
                        self.canvas.create_line(*sum(pts, ()), fill=col, width=w,
                                                capstyle="round", joinstyle="round",
                                                smooth=True, tags="stroke")
                elif kind == "dot":
                    _, x, y, r, col = op
                    self.canvas.create_oval(x - r, y - r, x + r, y + r,
                                            fill=col, outline="", tags="stroke")
                elif kind == "emoji":
                    _, x, y, char, px = op
                    self.canvas.create_image(x, y, image=self._emoji_photo(char, px),
                                             tags="stroke")
        self._tick_id = self.win.after(FRAME_MS, self._tick)

    # -- gif rendering ------------------------------------------------------
    def _render_frames(self):
        frames = []
        for f in range(N_FRAMES):
            im = self.image.convert("RGBA")
            if self.watermark:
                g = self.watermark["frames_img"][f]
                im.alpha_composite(g, (int(self.watermark["cx"] - g.width / 2),
                                       int(self.watermark["cy"] - g.height / 2)))
            d = ImageDraw.Draw(im)
            for s in self.strokes:
                for op in s["ops"][f]:
                    kind = op[0]
                    if kind == "line":
                        _, pts, col, w = op
                        if len(pts) < 2:
                            continue
                        d.line(pts, fill=col, width=w, joint="curve")
                        r = w / 2
                        for (x, y) in (pts[0], pts[-1]):
                            d.ellipse((x - r, y - r, x + r, y + r), fill=col)
                    elif kind == "dot":
                        _, x, y, r, col = op
                        d.ellipse((x - r, y - r, x + r, y + r), fill=col)
                    elif kind == "emoji":
                        _, x, y, char, px = op
                        g = emoji_image(char, int(px))
                        im.alpha_composite(g, (int(x - g.width / 2),
                                               int(y - g.height / 2)))
            frames.append(im.convert("RGB"))
        return frames

    def _write_gif(self, path):
        frames = self._render_frames()
        pframes = [fr.convert("P", palette=Image.ADAPTIVE, colors=256) for fr in frames]
        pframes[0].save(path, save_all=True, append_images=pframes[1:],
                        loop=0, duration=FRAME_MS, disposal=2, optimize=True)
        return frames[0]

    def copy_gif(self):
        path = os.path.join(tempfile.gettempdir(), "SnipSquiggle.gif")
        try:
            static = self._write_gif(path)
            set_clipboard(path, static)
        except Exception as ex:
            messagebox.showerror("Copy failed", str(ex), parent=self.win)
            return
        self._flash(self.copy_btn, "✓ Copied!")

    def save_gif(self):
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".gif",
                filetypes=[("GIF", "*.gif")], initialfile="snip.gif")
        if not path:
            return
        try:
            self._write_gif(path)
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex), parent=self.win)

    def _flash(self, btn, text, ms=1200):
        old = btn["text"]
        btn.configure(text=text)
        btn.after(ms, lambda: self._restore(btn, old))

    def _toast(self, text, ms=4000):
        """Transient message in the toolbar's status slot."""
        self.status.configure(text=text)
        self.status.after(ms, lambda: self._restore(self.status, ""))

    def _restore(self, widget, text):
        if not self._closed:      # the window may be gone by now
            widget.configure(text=text)

    # -- lifecycle ----------------------------------------------------------
    def new_snip(self):
        # The controller owns the teardown (it has to know an editor is going
        # away), so just ask it for a new snip — it calls discard() on us.
        self.new_snip_cb()

    def discard(self):
        """Close without firing on_close — the controller is replacing us."""
        self._teardown()

    def _teardown(self):
        """Stop the animation loop, then destroy the window.

        The loop must be cancelled first: a pending ``after`` callback outlives
        ``destroy()`` and would blow up on the dead canvas (harmless in one-shot
        mode where the mainloop exits too, fatal-looking in tray mode)."""
        self._closed = True
        if self._tick_id is not None:
            try:
                self.win.after_cancel(self._tick_id)
            except Exception:
                pass
            self._tick_id = None
        self.win.destroy()

    def _quit(self):
        self._teardown()
        self.on_close()


# ===========================================================================
# System tray + global PrintScreen hotkey (Windows, resident mode)
# ===========================================================================
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")


def _log(msg):
    """Diagnostics for tray mode (visible when launched from a console).

    Under pythonw.exe there is no console, so sys.stderr is None — writing to
    it would raise and abort tray startup. Degrade to a no-op in that case.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(f"[SnipSquiggle] {msg}\n")
        stream.flush()
    except (OSError, ValueError):
        pass


class WinTray:
    """A system-tray icon plus a global PrintScreen hotkey (Windows only).

    Win32 hotkeys and tray callbacks need a live message loop, so this runs on
    its own daemon thread with a hidden window. Because tkinter is not
    thread-safe, we never touch Tk from here — events are pushed onto a
    ``queue.Queue`` that the Tk main thread drains via ``App._poll_events``.

    Queue messages: "snip", "quit", "hotkey_failed".
    """

    HOTKEY_ID = 0xB001
    ID_SNIP = 1001
    ID_QUIT = 1002

    def __init__(self, events, icon_path=ICON_PATH):
        import win32con
        self._WM_TRAY = win32con.WM_APP + 1
        self.events = events
        self.icon_path = icon_path
        self.hwnd = None
        self._thread = threading.Thread(target=self._run, name="snip-tray",
                                        daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        import win32con, win32gui
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    # -- runs on the tray thread --------------------------------------------
    def _run(self):
        import win32api, win32con, win32gui

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "SnipSquiggleTray"
        wc.lpfnWndProc = self._wndproc
        class_atom = win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(class_atom, "SnipSquiggle", 0,
                                          0, 0, 0, 0, 0, 0, wc.hInstance, None)
        win32gui.UpdateWindow(self.hwnd)

        self._add_icon()

        # PrintScreen (VK_SNAPSHOT), no modifiers. May fail if another app or
        # the Windows 11 "Print screen opens Snipping Tool" setting owns it.
        try:
            win32gui.RegisterHotKey(self.hwnd, self.HOTKEY_ID, 0,
                                    win32con.VK_SNAPSHOT)
            _log("PrintScreen hotkey registered — press PrintScreen to snip.")
        except win32gui.error as e:
            _log(f"Could NOT register PrintScreen hotkey ({e}); "
                 "another app or Windows owns the key. Use the tray icon.")
            self.events.put("hotkey_failed")

        win32gui.PumpMessages()

    def _add_icon(self):
        import win32con, win32gui
        try:
            hicon = win32gui.LoadImage(0, self.icon_path, win32con.IMAGE_ICON,
                                       0, 0, win32con.LR_LOADFROMFILE)
        except Exception:
            hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        nid = (self.hwnd, 0, flags, self._WM_TRAY, hicon,
               "SnipSquiggle — press PrintScreen to snip")
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        import win32api, win32con, win32gui
        if msg == win32con.WM_HOTKEY and wparam == self.HOTKEY_ID:
            self.events.put("snip")
            return 0
        if msg == self._WM_TRAY:
            if lparam == win32con.WM_LBUTTONDBLCLK:
                self.events.put("snip")
            elif lparam == win32con.WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == win32con.WM_COMMAND:
            cid = win32api.LOWORD(wparam)
            if cid == self.ID_SNIP:
                self.events.put("snip")
            elif cid == self.ID_QUIT:
                self.events.put("quit")
            return 0
        if msg == win32con.WM_DESTROY:
            try:
                win32gui.UnregisterHotKey(hwnd, self.HOTKEY_ID)
            except win32gui.error:
                pass
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _show_menu(self):
        import win32con, win32gui
        menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_SNIP, "Snip now")
        win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(menu, win32con.MF_STRING, self.ID_QUIT, "Quit")
        x, y = win32gui.GetCursorPos()
        win32gui.SetForegroundWindow(self.hwnd)   # so the menu auto-dismisses
        win32gui.TrackPopupMenu(menu, win32con.TPM_RIGHTALIGN | win32con.TPM_BOTTOMALIGN,
                                x, y, 0, self.hwnd, None)
        win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)


class MacTray:
    """A menu-bar icon plus a global hotkey (macOS only).

    Unlike WinTray this spawns NO thread: Cocoa objects (NSStatusItem) and the
    Carbon hotkey handler must live on the main thread and be serviced by the
    main run loop — which tkinter's ``mainloop`` already pumps on macOS. So we
    just install everything on the calling (main) thread and, like WinTray,
    push events onto the shared queue for ``App._poll_events`` to drain.

    macOS has no PrintScreen key and reserves Cmd+Shift+3/4/5 for its own
    screenshots, so the default combo here is **Cmd+Shift+2**.

    Queue messages: "snip", "quit", "hotkey_failed".
    """

    KEYCODE = 0x13                    # kVK_ANSI_2
    MODIFIERS = 0x0100 | 0x0200       # cmdKey | shiftKey
    HOTKEY_LABEL = "⌘⇧2"    # ⌘⇧2

    def __init__(self, events):
        self.events = events
        self._status_item = None
        self._delegate = None
        self._carbon = None
        self._hk_ref = ctypes.c_void_p()
        self._handler_ref = ctypes.c_void_p()
        self._hk_callback = None      # keep the CFUNCTYPE alive (else GC'd)

    def start(self):
        try:
            self._install_menubar()
        except Exception as e:               # menu bar is nice-to-have
            _log(f"macOS: menu-bar icon failed ({e}); hotkey still active.")
        self._install_hotkey()

    def stop(self):
        try:
            if self._status_item is not None:
                from AppKit import NSStatusBar
                NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
        except Exception:
            pass
        try:
            if self._carbon is not None and self._hk_ref:
                self._carbon.UnregisterEventHotKey(self._hk_ref)
        except Exception:
            pass

    # -- menu bar (Cocoa) ---------------------------------------------------
    def _install_menubar(self):
        from AppKit import (NSStatusBar, NSMenu, NSMenuItem,
                            NSVariableStatusItemLength)
        from Foundation import NSObject

        events = self.events

        class _Delegate(NSObject):
            def snip_(self, sender):
                events.put("snip")

            def quitApp_(self, sender):
                events.put("quit")

        self._delegate = _Delegate.alloc().init()

        item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        try:
            item.button().setTitle_("\U0001f4a7")   # 💧 (matches the logo)
        except Exception:
            item.setTitle_("\U0001f4a7")

        menu = NSMenu.alloc().init()
        snip = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Snip now ({self.HOTKEY_LABEL})", "snip:", "")
        snip.setTarget_(self._delegate)
        menu.addItem_(snip)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_ = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit SnipSquiggle", "quitApp:", "")
        quit_.setTarget_(self._delegate)
        menu.addItem_(quit_)
        item.setMenu_(menu)
        self._status_item = item

    # -- global hotkey (Carbon via ctypes) ----------------------------------
    def _install_hotkey(self):
        import ctypes.util

        carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))
        self._carbon = carbon

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32),
                        ("eventKind", ctypes.c_uint32)]

        class EventHotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p)

        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p, HANDLER, ctypes.c_uint32,
            ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p)]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.UnregisterEventHotKey.restype = ctypes.c_int32

        events = self.events

        def _on_hotkey(next_handler, event, user_data):
            events.put("snip")
            return 0                      # noErr

        self._hk_callback = HANDLER(_on_hotkey)

        target = carbon.GetApplicationEventTarget()
        spec = EventTypeSpec(0x6b657962, 5)   # kEventClassKeyboard, ...HotKeyPressed
        err = carbon.InstallEventHandler(target, self._hk_callback, 1,
                                         ctypes.byref(spec), None,
                                         ctypes.byref(self._handler_ref))
        if err != 0:
            _log(f"macOS: InstallEventHandler failed ({err}).")
            self.events.put("hotkey_failed")
            return

        hk_id = EventHotKeyID(0x736e6970, 1)  # 'snip', 1
        err = carbon.RegisterEventHotKey(self.KEYCODE, self.MODIFIERS, hk_id,
                                         target, 0, ctypes.byref(self._hk_ref))
        if err != 0:
            _log(f"macOS: RegisterEventHotKey failed ({err}); "
                 f"{self.HOTKEY_LABEL} may be taken by another app.")
            self.events.put("hotkey_failed")
        else:
            _log(f"macOS: global hotkey {self.HOTKEY_LABEL} registered.")


# ===========================================================================
# App controller
# ===========================================================================
class App:
    def __init__(self, resident=False, auto_copy=True):
        self.root = tk.Tk()
        self.root.withdraw()
        self.resident = resident and (IS_WIN or IS_MAC)
        self.auto_copy = auto_copy  # copy the plain snip as soon as it's taken
        self.state = "idle"        # idle | capturing | editing
        self.editor = None         # the open Editor, if state == "editing"
        self.tray = None

        if resident and IS_LINUX:
            sys.stderr.write("--tray is only implemented on Windows and macOS; "
                             "running a single snip instead.\n")

        if self.resident:
            _log("Tray mode started; sitting idle. Use the tray/menu-bar icon "
                 "for options.")
            self.events = queue.Queue()
            self.tray = WinTray(self.events) if IS_WIN else MacTray(self.events)
            self.tray.start()
            self._poll_events()    # sit idle until the hotkey / tray triggers
        else:
            self.request_capture()

    # -- resident event pump (Tk thread) ------------------------------------
    def _poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                _log(f"event: {ev}" + (" (ignored, region select in progress)"
                                       if ev == "snip" and
                                       self.state == "capturing" else ""))
                if ev == "snip":
                    self.request_capture()
                elif ev == "quit":
                    self._shutdown()
                    return
                elif ev == "hotkey_failed":
                    self._warn_hotkey_failed()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def request_capture(self):
        """Single entry point for "snip now" — hotkey, tray menu, ＋ New, Ctrl+N.

        An open editor is thrown away and replaced (same as ＋ New), so the
        hotkey works while you're annotating. Presses during region select are
        dropped: the overlay is already up and waiting for a drag.
        """
        if self.state == "capturing":
            return
        if self.editor is not None:
            self.editor.discard()
            self.editor = None
        self.state = "capturing"
        self.start_capture()

    def _warn_hotkey_failed(self):
        if IS_MAC:
            msg = ("Couldn't register the Cmd+Shift+2 hotkey — another app may "
                   "already own it.\n\n"
                   "You can still snip from the menu-bar icon, or change the "
                   "combo (MacTray.KEYCODE / MODIFIERS) and restart.")
        else:
            msg = ("Couldn't grab the PrintScreen key — another app (often the "
                   "Windows 11 \"Use Print screen to open Snipping Tool\" "
                   "setting) already owns it.\n\n"
                   "Turn that off under Settings → Accessibility → Keyboard, "
                   "then restart SnipSquiggle. You can still snip from the "
                   "tray icon.")
        messagebox.showwarning("SnipSquiggle", msg)

    def _shutdown(self):
        if self.tray:
            self.tray.stop()
        self.root.quit()

    # -- capture / edit flow ------------------------------------------------
    def start_capture(self):
        self.root.after(120, self._do_capture)

    def _do_capture(self):
        if IS_MAC:
            self._captured(mac_screencapture())
        else:
            OverlayCapture(self.root, self._captured)

    def _captured(self, image):
        if image is None:
            self._finish()
            return
        copied = self._copy_static(image)
        self.state = "editing"
        self.editor = Editor(self.root, image, self.request_capture,
                             self._finish, static_copied=copied)

    def _copy_static(self, image):
        """Put the raw snip on the clipboard. Best effort — a clipboard failure
        must not cost the user their snip, so it's logged, not raised."""
        if not self.auto_copy:
            return False
        try:
            set_clipboard_image(image)
        except Exception as ex:
            _log(f"auto-copy of the static snip failed: {ex}")
            return False
        return True

    def _finish(self):
        """A snip cycle ended (cancelled or editor closed)."""
        self.editor = None
        self.state = "idle"       # resident: back to idle, tray + hotkey live
        if not self.resident:
            self.root.quit()

    def run(self):
        self.root.mainloop()


def _check_tk():
    """Apple's system Tk 8.5 segfaults for GUI apps — bail with guidance."""
    if IS_MAC and tk.TkVersion < 8.6:
        sys.stderr.write(
            "\nSnipSquiggle needs Tk 8.6+, but this Python is linked against "
            f"Tk {tk.TkVersion} (Apple's system Tk), which crashes GUI apps.\n"
            "Run it under a Python with modern Tk instead, e.g.:\n"
            "  brew install python-tk\n"
            "  \"$(brew --prefix)/bin/python3\" snipsquiggle.py\n"
            "or install Python from https://www.python.org (bundles Tk 8.6).\n\n")
        sys.exit(1)


if __name__ == "__main__":
    _check_tk()
    resident = "--tray" in sys.argv or "--resident" in sys.argv
    auto_copy = "--no-copy" not in sys.argv
    App(resident=resident, auto_copy=auto_copy).run()
