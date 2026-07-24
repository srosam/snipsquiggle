"""
SnipSquiggle - a Snipping-Tool-style capture + animated annotation app.

Flow:
  1. Launch -> screen dims, drag a rectangle to snip (Esc cancels).
  2. Editor opens with your snip. Draw with pen / arrow / box, and pick an
     animation style for each stroke:
        Boil  - hand-drawn squiggle that gently wobbles (default)
        Ants  - marching-ants moving dashes
        Dots  - dots flowing along the stroke
        Emoji - emojis marching along the stroke (🔥 ❤️ ⭐ ...)
  3. Ctrl+C copies a looping animated GIF to the clipboard.
     (Pastes as an animated file into Slack/Discord/Teams/Explorer,
      and as a static image everywhere else.)

Deps: pillow, pywin32   (see requirements.txt)
"""

import io
import os
import math
import bisect
import random
import tempfile
import ctypes
from ctypes import wintypes

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox
from PIL import Image, ImageTk, ImageGrab, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Make the process DPI-aware so tkinter pixel coords match the physical
# screenshot pixels (essential on scaled Windows displays).
# ---------------------------------------------------------------------------
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
EMOJIS = ["🔥", "❤️", "⭐", "✅", "👍", "😂", "🎉", "➡️", "💯", "👀"]
EMOJI_FONT_PATH = r"C:\Windows\Fonts\seguiemj.ttf"


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
    """Positions every `spacing` px along the path, shifted by phase."""
    L = cl[-1]
    out = []
    s = (-phase) % spacing
    while s <= L:
        out.append(point_at(pts, cl, s))
        s += spacing
    if not out:
        out.append(point_at(pts, cl, L / 2))
    return out


# ---------------------------------------------------------------------------
# Emoji rasterisation (color glyphs via Segoe UI Emoji), cached
# ---------------------------------------------------------------------------
_FONT_CACHE = {}
_EMOJI_CACHE = {}


def emoji_font(px):
    if px not in _FONT_CACHE:
        try:
            _FONT_CACHE[px] = ImageFont.truetype(EMOJI_FONT_PATH, px)
        except Exception:
            _FONT_CACHE[px] = ImageFont.load_default()
    return _FONT_CACHE[px]


def emoji_image(char, px):
    key = (char, px)
    if key in _EMOJI_CACHE:
        return _EMOJI_CACHE[key]
    box = int(px * 1.3)
    img = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        d.text((box / 2, box / 2), char, font=emoji_font(px),
               anchor="mm", embedded_color=True)
    except Exception:
        d.text((box / 2, box / 2), char, font=emoji_font(px),
               anchor="mm", fill=(0, 0, 0, 255))
    _EMOJI_CACHE[key] = img
    return img


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

    # ants / dots / emoji all march along one fixed, gently-wobbled path
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

    else:  # fallback: plain wobble
        for _ in range(N_FRAMES):
            frames.append([("line", pl, color, width) for pl, _cl in metas])

    return frames


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
    def __init__(self, root, image, new_snip_cb):
        self.root = root
        self.image = image.convert("RGB")
        self.new_snip_cb = new_snip_cb

        self.color = "#ff3b30"
        self.width = 5
        self.tool = "pen"          # pen | arrow | box
        self.anim = "squiggle"     # squiggle | ants | dots | emoji
        self.emoji = "🔥"
        self.strokes = []          # each: {ops:[frames], meta...}
        self.frame = 0
        self._seed = 0
        self._live_pts = []
        self._live_start = None
        self._emoji_photos = {}    # (char,px) -> ImageTk.PhotoImage (keep refs)

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
    def _btn(self, parent, text, cmd, **kw):
        b = tk.Button(parent, text=text, command=cmd, relief="flat",
                      bg="#2d2d2d", fg="white", activebackground="#3d3d3d",
                      activeforeground="white", bd=0, padx=10, pady=4,
                      font=("Segoe UI", 9), **kw)
        b.pack(side="left", padx=2)
        return b

    def _sep(self, parent):
        tk.Frame(parent, width=1, bg="#444").pack(side="left", fill="y", padx=6)

    def _label(self, parent, text):
        tk.Label(parent, text=text, bg="#1e1e1e", fg="#9a9a9a",
                 font=("Segoe UI", 9)).pack(side="left", padx=(4, 2))

    def _build_toolbar(self):
        # Row 1: tools / colors / sizes / undo / actions
        bar = tk.Frame(self.win, bg="#1e1e1e")
        bar.pack(fill="x", padx=10, pady=(8, 2))

        self.tool_btns = {}
        for name, label in (("pen", "✎ Pen"), ("arrow", "↗ Arrow"), ("box", "▭ Box")):
            self.tool_btns[name] = self._btn(bar, label, lambda n=name: self.set_tool(n))
        self._sep(bar)
        for c in PALETTE:
            tk.Button(bar, bg=c, width=2, relief="flat", bd=1,
                      command=lambda cc=c: self.set_color(cc)).pack(side="left", padx=1)
        self._btn(bar, "＋", self.pick_color)
        self._sep(bar)
        for w, lbl in ((3, "S"), (5, "M"), (9, "L"), (14, "XL")):
            self._btn(bar, lbl, lambda ww=w: self.set_width(ww))
        self._sep(bar)
        self._btn(bar, "↶ Undo", self.undo)
        self._btn(bar, "✕ Clear", self.clear)

        right = tk.Frame(bar, bg="#1e1e1e")
        right.pack(side="right")
        self._btn(right, "＋ New (Ctrl+N)", self.new_snip)
        self._btn(right, "💾 Save (Ctrl+S)", self.save_gif)
        self.copy_btn = tk.Button(right, text="📋 Copy GIF (Ctrl+C)",
                                  command=self.copy_gif, relief="flat",
                                  bg="#0a84ff", fg="white", bd=0, padx=12, pady=4,
                                  font=("Segoe UI", 9, "bold"),
                                  activebackground="#0060df", activeforeground="white")
        self.copy_btn.pack(side="left", padx=2)

        # Row 2: animation style + emoji picker
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
            b = tk.Button(bar2, text=ch, command=lambda c=ch: self.set_emoji(c),
                          relief="flat", bg="#2d2d2d", bd=0, padx=4, pady=2,
                          font=("Segoe UI Emoji", 12))
            b.pack(side="left", padx=1)
            self.emoji_btns[ch] = b

        self._refresh_btns()

    def _refresh_btns(self):
        for name, b in self.tool_btns.items():
            b.configure(bg="#0a84ff" if name == self.tool else "#2d2d2d")
        for name, b in self.anim_btns.items():
            b.configure(bg="#0a84ff" if name == self.anim else "#2d2d2d")
        for ch, b in self.emoji_btns.items():
            on = (self.anim == "emoji" and ch == self.emoji)
            b.configure(bg="#0a84ff" if on else "#2d2d2d")

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
        self.frame = (self.frame + 1) % N_FRAMES
        self.canvas.delete("stroke")
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
        self.win.after(FRAME_MS, self._tick)

    # -- gif rendering ------------------------------------------------------
    def _render_frames(self):
        frames = []
        for f in range(N_FRAMES):
            im = self.image.convert("RGBA")
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
