"""
SnipSquiggle - a Snipping-Tool-style capture + squiggly animated annotation app.

Flow:
  1. Launch -> screen dims, drag a rectangle to snip (Esc cancels).
  2. Editor opens with your snip. Draw with pen / arrow / box.
     Every stroke is drawn "squiggly" and animates with a boiling-line wobble.
  3. Ctrl+C copies a looping animated GIF to the clipboard.
     (Pastes as an animated file into Slack/Discord/Teams/Explorer,
      and as a static image everywhere else.)

Deps: pillow, pywin32   (see requirements.txt)
"""

import io
import os
import math
import random
import tempfile
import ctypes
from ctypes import wintypes

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
from PIL import Image, ImageTk, ImageGrab, ImageDraw

# ---------------------------------------------------------------------------
# Make the process DPI-aware so tkinter pixel coords match the physical
# screenshot pixels (essential on scaled Windows displays).
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2-ish
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Animation / squiggle tuning
# ---------------------------------------------------------------------------
N_FRAMES = 3          # number of boiling-line variants
FRAME_MS = 120        # on-screen animation speed (and gif frame delay)
RESAMPLE_SPACING = 7  # px between points before we add wobble
JITTER_BASE = 1.8     # base wobble amplitude in px

PALETTE = ["#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#000000", "#ffffff"]


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def resample(points, spacing=RESAMPLE_SPACING):
    """Evenly space a polyline by arc length so wobble looks uniform."""
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
    """Turn two corners into a closed rectangle path, resampled."""
    x0, y0 = p0
    x1, y1 = p1
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    out = []
    for a, b in zip(corners, corners[1:]):
        seg = resample([a, b], spacing=RESAMPLE_SPACING)
        out.extend(seg if not out else seg[1:])
    return out


def arrow_polylines(p0, p1, width):
    """A straight-ish arrow: shaft plus two head barbs (each resampled)."""
    shaft = resample([p0, p1])
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    head = max(12, width * 4)
    spread = math.radians(28)
    b1 = (p1[0] - head * math.cos(ang - spread), p1[1] - head * math.sin(ang - spread))
    b2 = (p1[0] - head * math.cos(ang + spread), p1[1] - head * math.sin(ang + spread))
    return [shaft, resample([p1, b1]), resample([p1, b2])]


def make_variants(polylines, width, n_frames=N_FRAMES):
    """Precompute N jittered copies of every polyline for the boiling effect."""
    amp = JITTER_BASE + width * 0.35
    variants = []
    for f in range(n_frames):
        rnd = random.Random(1000 * f + 7)
        frame = []
        for pl in polylines:
            jl = []
            for i, (x, y) in enumerate(pl):
                # endpoints wobble a little less so shapes stay anchored
                a = amp * (0.4 if (i == 0 or i == len(pl) - 1) else 1.0)
                jl.append((x + rnd.uniform(-a, a), y + rnd.uniform(-a, a)))
            frame.append(jl)
        variants.append(frame)
    return variants


# ---------------------------------------------------------------------------
# Windows clipboard (raw, via ctypes so 64-bit handles are safe)
# ---------------------------------------------------------------------------
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

GMEM_MOVEABLE = 0x0002
CF_DIB = 8
CF_HDROP = 15


def _global_from_bytes(data: bytes):
    h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    ptr = _k32.GlobalLock(h)
    ctypes.memmove(ptr, data, len(data))
    _k32.GlobalUnlock(h)
    return h


def _hdrop_bytes(paths):
    import struct
    header = struct.pack("<IiiiI", 20, 0, 0, 0, 1)  # DROPFILES, fWide=1
    body = "".join(p + "\0" for p in paths) + "\0"
    return header + body.encode("utf-16-le")


def _dib_bytes(im: Image.Image):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "BMP")
    return buf.getvalue()[14:]  # strip BITMAPFILEHEADER -> CF_DIB


def set_clipboard(gif_path, static_frame):
    """Put GIF-as-file + static bitmap + raw GIF bytes on the clipboard."""
    cf_gif = _u32.RegisterClipboardFormatW("GIF")
    with open(gif_path, "rb") as f:
        gif_bytes = f.read()

    payloads = {
        CF_HDROP: _hdrop_bytes([gif_path]),
        CF_DIB: _dib_bytes(static_frame),
        cf_gif: gif_bytes,
    }
    if not _u32.OpenClipboard(None):
        raise OSError("Could not open clipboard")
    try:
        _u32.EmptyClipboard()
        for fmt, data in payloads.items():
            _u32.SetClipboardData(fmt, _global_from_bytes(data))
    finally:
        _u32.CloseClipboard()


# ===========================================================================
# Screen capture overlay
# ===========================================================================
class Capture:
    def __init__(self, root, on_done):
        self.root = root
        self.on_done = on_done

        self.shot = ImageGrab.grab(all_screens=True)
        vx = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = ctypes.windll.user32.GetSystemMetrics(77)
        self.vx, self.vy = vx, vy
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

        # dim overlay (4 rects around the selection) + selection outline
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
        x1, y1 = e.x, e.y
        lx, ty = min(x0, x1), min(y0, y1)
        rx, by = max(x0, x1), max(y0, y1)
        self.canvas.coords(self.sel, lx, ty, rx, by)
        W = self.shot.width
        H = self.shot.height
        # top, bottom, left, right dim panels around the selection
        self.canvas.coords(self.dim[0], 0, 0, W, ty)
        self.canvas.coords(self.dim[1], 0, by, W, H)
        self.canvas.coords(self.dim[2], 0, ty, lx, by)
        self.canvas.coords(self.dim[3], rx, ty, W, by)

    def _up(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x, e.y
        lx, ty = min(x0, x1), min(y0, y1)
        rx, by = max(x0, x1), max(y0, y1)
        self.win.destroy()
        if rx - lx < 5 or by - ty < 5:
            self.on_done(None)
            return
        crop = self.shot.crop((lx, ty, rx, by))
        self.on_done(crop)

    def _cancel(self):
        self.win.destroy()
        self.on_done(None)


# ===========================================================================
# Editor
# ===========================================================================
class Editor:
    def __init__(self, root, image, new_snip_cb):
        self.root = root
        self.image = image.convert("RGB")
        self.new_snip_cb = new_snip_cb

        self.color = "#ff3b30"
        self.width = 5
        self.tool = "pen"          # pen | arrow | box
        self.strokes = []          # each: {polylines, variants, color, width}
        self.frame = 0
        self._live_pts = []
        self._live_start = None

        self.win = tk.Toplevel(root)
        self.win.title("SnipSquiggle")
        self.win.configure(bg="#1e1e1e")
        self.win.protocol("WM_DELETE_WINDOW", self._quit)

        self._build_toolbar()

        self.tkimg = ImageTk.PhotoImage(self.image)
        self.canvas = tk.Canvas(self.win, width=self.image.width,
                                height=self.image.height,
                                highlightthickness=0, bd=0, cursor="pencil")
        self.canvas.pack(padx=10, pady=(0, 10))
        self.canvas.create_image(0, 0, anchor="nw", image=self.tkimg, tags="bg")

        self.canvas.bind("<Button-1>", self._down)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._up)

        self.win.bind("<Control-c>", lambda e: self.copy_gif())
        self.win.bind("<Control-s>", lambda e: self.save_gif())
        self.win.bind("<Control-z>", lambda e: self.undo())
        self.win.bind("<Control-n>", lambda e: self.new_snip())
        self.win.bind("<Escape>", lambda e: self._quit())

        self.win.after(200, self.win.focus_force)
        self._tick()

    # -- toolbar ------------------------------------------------------------
    def _build_toolbar(self):
        bar = tk.Frame(self.win, bg="#1e1e1e")
        bar.pack(fill="x", padx=10, pady=8)

        def btn(parent, text, cmd, **kw):
            b = tk.Button(parent, text=text, command=cmd, relief="flat",
                          bg="#2d2d2d", fg="white", activebackground="#3d3d3d",
                          activeforeground="white", bd=0, padx=10, pady=4,
                          font=("Segoe UI", 9), **kw)
            b.pack(side="left", padx=2)
            return b

        # tools
        self.tool_btns = {}
        for name, label in (("pen", "✎ Pen"), ("arrow", "↗ Arrow"),
                            ("box", "▭ Box")):
            self.tool_btns[name] = btn(bar, label,
                                       lambda n=name: self.set_tool(n))
        tk.Frame(bar, width=1, bg="#444").pack(side="left", fill="y", padx=6)

        # colors
        for c in PALETTE:
            sw = tk.Button(bar, bg=c, width=2, relief="flat", bd=1,
                           command=lambda cc=c: self.set_color(cc))
            sw.pack(side="left", padx=1)
        btn(bar, "＋", self.pick_color)
        tk.Frame(bar, width=1, bg="#444").pack(side="left", fill="y", padx=6)

        # sizes
        for w, lbl in ((3, "S"), (5, "M"), (9, "L"), (14, "XL")):
            btn(bar, lbl, lambda ww=w: self.set_width(ww))
        tk.Frame(bar, width=1, bg="#444").pack(side="left", fill="y", padx=6)

        btn(bar, "↶ Undo", self.undo)
        btn(bar, "✕ Clear", self.clear)

        # right side actions
        right = tk.Frame(bar, bg="#1e1e1e")
        right.pack(side="right")
        btn(right, "＋ New (Ctrl+N)", self.new_snip)
        btn(right, "💾 Save GIF (Ctrl+S)", self.save_gif)
        self.copy_btn = tk.Button(right, text="📋 Copy GIF (Ctrl+C)",
                                  command=self.copy_gif, relief="flat",
                                  bg="#0a84ff", fg="white", bd=0, padx=12,
                                  pady=4, font=("Segoe UI", 9, "bold"),
                                  activebackground="#0060df",
                                  activeforeground="white")
        self.copy_btn.pack(side="left", padx=2)

        self._refresh_tool_btns()

    def _refresh_tool_btns(self):
        for name, b in self.tool_btns.items():
            b.configure(bg="#0a84ff" if name == self.tool else "#2d2d2d")

    def set_tool(self, name):
        self.tool = name
        self._refresh_tool_btns()

    def set_color(self, c):
        self.color = c

    def pick_color(self):
        c = colorchooser.askcolor(color=self.color, parent=self.win)[1]
        if c:
            self.color = c

    def set_width(self, w):
        self.width = w

    # -- drawing ------------------------------------------------------------
    def _down(self, e):
        self._live_start = (e.x, e.y)
        self._live_pts = [(e.x, e.y)]

    def _move(self, e):
        if self._live_start is None:
            return
        p = (e.x, e.y)
        self._live_pts.append(p)
        self.canvas.delete("live")
        if self.tool == "pen":
            if len(self._live_pts) >= 2:
                self.canvas.create_line(*sum(self._live_pts, ()),
                                        fill=self.color, width=self.width,
                                        capstyle="round", joinstyle="round",
                                        smooth=True, tags="live")
        elif self.tool == "box":
            x0, y0 = self._live_start
            self.canvas.create_rectangle(x0, y0, p[0], p[1], outline=self.color,
                                         width=self.width, tags="live")
        else:  # arrow
            x0, y0 = self._live_start
            self.canvas.create_line(x0, y0, p[0], p[1], fill=self.color,
                                    width=self.width, capstyle="round",
                                    arrow="last", tags="live")

    def _up(self, e):
        if self._live_start is None:
            return
        self.canvas.delete("live")
        start = self._live_start
        end = (e.x, e.y)
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

        self.strokes.append({
            "polylines": polylines,
            "variants": make_variants(polylines, self.width),
            "color": self.color,
            "width": self.width,
        })

    def undo(self):
        if self.strokes:
            self.strokes.pop()

    def clear(self):
        self.strokes.clear()

    # -- animation loop -----------------------------------------------------
    def _tick(self):
        self.frame = (self.frame + 1) % N_FRAMES
        self.canvas.delete("stroke")
        for s in self.strokes:
            for pl in s["variants"][self.frame]:
                if len(pl) < 2:
                    continue
                self.canvas.create_line(*sum(pl, ()), fill=s["color"],
                                        width=s["width"], capstyle="round",
                                        joinstyle="round", smooth=True,
                                        tags="stroke")
        self.win.after(FRAME_MS, self._tick)

    # -- gif rendering ------------------------------------------------------
    def _render_frames(self):
        frames = []
        for f in range(N_FRAMES):
            im = self.image.copy()
            d = ImageDraw.Draw(im)
            for s in self.strokes:
                col = s["color"]
                w = s["width"]
                for pl in s["variants"][f]:
                    if len(pl) < 2:
                        continue
                    d.line(pl, fill=col, width=w, joint="curve")
                    # round caps
                    r = w / 2
                    for (x, y) in (pl[0], pl[-1]):
                        d.ellipse((x - r, y - r, x + r, y + r), fill=col)
            frames.append(im)
        return frames

    def _write_gif(self, path):
        frames = self._render_frames()
        pframes = [fr.convert("P", palette=Image.ADAPTIVE, colors=256)
                   for fr in frames]
        pframes[0].save(path, save_all=True, append_images=pframes[1:],
                        loop=0, duration=FRAME_MS, disposal=2, optimize=False)
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
        path = filedialog.asksaveasfilename(parent=self.win,
                defaultextension=".gif", filetypes=[("GIF", "*.gif")],
                initialfile="snip.gif")
        if not path:
            return
        try:
            self._write_gif(path)
        except Exception as ex:
            messagebox.showerror("Save failed", str(ex), parent=self.win)

    def _flash(self, btn, text, ms=1200):
        old = btn["text"]
        btn.configure(text=text)
        btn.after(ms, lambda: btn.configure(text=old))

    # -- lifecycle ----------------------------------------------------------
    def new_snip(self):
        self.win.destroy()
        self.new_snip_cb()

    def _quit(self):
        self.win.destroy()
        self.root.quit()


# ===========================================================================
# App controller
# ===========================================================================
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.start_capture()

    def start_capture(self):
        # give the window manager a beat to hide any prior editor before grab
        self.root.after(120, self._do_capture)

    def _do_capture(self):
        Capture(self.root, self._captured)

    def _captured(self, image):
        if image is None:
            self.root.quit()
            return
        Editor(self.root, image, self.start_capture)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
