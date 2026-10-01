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
  --no-save         Don't auto-save snips to disk.
  --save-dir DIR    Where auto-saved snips go (default ~/Pictures/SnipSquiggle).

Flow:
  1. Launch (or press the hotkey in --tray mode) -> select a region to snip.
       Windows/Linux: a dimmed overlay, drag a rectangle (Esc cancels).
       macOS:         the native `screencapture -i` crosshair.
  1b. The plain snip lands on the clipboard immediately as a static image, so
      you can paste right away without annotating (--no-copy disables this).
  1c. It is also written to disk immediately - before the editor even opens - as
      ~/Pictures/SnipSquiggle/snip-<date>-<time>.png, so a capture can never be
      lost. The editor header links to that file. Annotating writes a companion
      "..._2.gif" beside it, rewritten in place as you keep drawing.
  2. Editor opens with your snip. Draw with the pen or a shape (box, ellipse,
     line, arrow, double arrow, triangle, diamond, star, heart), and pick an
     animation style per stroke:
        Boil  - hand-drawn squiggle that gently wobbles (default)
        Ants  - marching-ants moving dashes
        Dots  - dots flowing along the stroke
        Emoji - emojis marching along the stroke (🔥 ❤️ ⭐ ... or "＋ Any",
                which opens the OS emoji picker so any emoji can be used)
     Add text in any installed font and size. The Move tool drags anything
     already drawn; selecting an item and changing a style restyles it.
     Right-click opens a menu of tools, shapes, styles and item actions.
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
import time
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
WIDTHS = ((3, "S"), (5, "M"), (9, "L"), (14, "XL"))
ANIMS = (("squiggle", "〰 Boil"), ("ants", "┅ Ants"), ("dots", "•• Dots"),
         ("emoji", "😀 Emoji"))

# Drag-to-draw shapes, in the order the Shape menu lists them.
SHAPES = (("box", "▭ Box"), ("ellipse", "◯ Ellipse"), ("line", "╱ Line"),
          ("arrow", "↗ Arrow"), ("darrow", "⇄ Double arrow"),
          ("triangle", "△ Triangle"), ("diamond", "◇ Diamond"),
          ("star", "☆ Star"), ("heart", "♡ Heart"))
SHAPE_LABELS = dict(SHAPES)
# Single-key tool shortcuts (ignored while typing text).
TOOL_KEYS = {"v": "move", "p": "pen", "t": "text", "b": "box", "e": "ellipse",
             "l": "line", "a": "arrow"}

TEXT_SIZES = (14, 18, 24, 32, 48, 64, 96)
# Text font name -> candidate file names, first hit wins. Covers the Windows
# file names, the macOS /System/Library/Fonts(/Supplemental) names and the
# Linux metric-compatible stand-ins. Fonts with no file found are hidden.
TEXT_FONTS = (
    ("Arial", ("arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf",
               "DejaVuSans.ttf")),
    ("Arial Bold", ("arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf",
                    "DejaVuSans-Bold.ttf")),
    ("Segoe UI", ("segoeui.ttf",)),
    ("Segoe UI Bold", ("segoeuib.ttf",)),
    ("Verdana", ("verdana.ttf", "Verdana.ttf")),
    ("Trebuchet MS", ("trebuc.ttf", "Trebuchet MS.ttf")),
    ("Georgia", ("georgia.ttf", "Georgia.ttf")),
    ("Times New Roman", ("times.ttf", "Times New Roman.ttf",
                         "LiberationSerif-Regular.ttf", "DejaVuSerif.ttf")),
    ("Courier New", ("cour.ttf", "Courier New.ttf",
                     "LiberationMono-Regular.ttf", "DejaVuSansMono.ttf")),
    ("Consolas", ("consola.ttf",)),
    ("Impact", ("impact.ttf", "Impact.ttf")),
    ("Comic Sans MS", ("comic.ttf", "Comic Sans MS.ttf")),
    ("Ink Free", ("Inkfree.ttf",)),
    ("Segoe Print", ("segoepr.ttf",)),
    ("Marker Felt", ("MarkerFelt.ttc",)),
    ("Chalkboard", ("Chalkboard.ttc",)),
)
DEFAULT_TEXT_FONT = "Arial Bold"
MAX_CUSTOM_EMOJI = 6   # recently picked "any emoji" choices kept in the bar
SEL_COLOR = "#0a84ff"  # selection outline around the item being moved

# tk.Button ignores bg/fg on macOS (native Aqua button), so we build toolbar
# controls from Labels, which honor colors on every platform.
UI_FONT = ("Segoe UI", 9) if IS_WIN else ("Helvetica", 12)
EMOJI_UI_FONT = ("Segoe UI Emoji", 12) if IS_WIN else ("Helvetica", 15)
BTN_BG = "#2d2d2d"
BTN_SEL = "#0a84ff"
LINK_FG = "#6fb2ff"


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


def polygon_polyline(corners):
    """A closed polygon, each edge resampled so its corners stay sharp."""
    corners = list(corners) + [corners[0]]
    out = []
    for a, b in zip(corners, corners[1:]):
        seg = resample([a, b])
        out.extend(seg if not out else seg[1:])
    return out


def rect_polyline(p0, p1):
    x0, y0 = p0
    x1, y1 = p1
    return polygon_polyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def _curve_polyline(p0, p1, fn, steps=96):
    """A closed parametric curve fn(t) -> (u, v) in [-1, 1], fitted to the
    p0-p1 box and resampled to even spacing."""
    cx, cy = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    rx, ry = abs(p1[0] - p0[0]) / 2, abs(p1[1] - p0[1]) / 2
    pts = []
    for i in range(steps + 1):
        u, v = fn(2 * math.pi * i / steps)
        pts.append((cx + u * rx, cy + v * ry))
    return resample(pts)


def ellipse_polyline(p0, p1):
    return _curve_polyline(p0, p1, lambda t: (math.cos(t), math.sin(t)))


def _heart(t):
    # The classic heart curve, scaled from its natural ~[-16,16]x[-17,12] box.
    x = 16 * math.sin(t) ** 3
    y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
    return x / 16, -(y + 2.5) / 14.5


def star_polyline(p0, p1, points=5, inner=0.45):
    cx, cy = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    rx, ry = abs(p1[0] - p0[0]) / 2, abs(p1[1] - p0[1]) / 2
    corners = []
    for i in range(points * 2):
        a = -math.pi / 2 + math.pi * i / points
        k = 1.0 if i % 2 == 0 else inner
        corners.append((cx + rx * k * math.cos(a), cy + ry * k * math.sin(a)))
    return polygon_polyline(corners)


def _arrow_head(tip, tail, width):
    ang = math.atan2(tip[1] - tail[1], tip[0] - tail[0])
    head = max(12, width * 4)
    spread = math.radians(28)
    b1 = (tip[0] - head * math.cos(ang - spread), tip[1] - head * math.sin(ang - spread))
    b2 = (tip[0] - head * math.cos(ang + spread), tip[1] - head * math.sin(ang + spread))
    return [resample([tip, b1]), resample([tip, b2])]


def arrow_polylines(p0, p1, width, both=False):
    out = [resample([p0, p1])] + _arrow_head(p1, p0, width)
    if both:
        out += _arrow_head(p0, p1, width)
    return out


def shape_polylines(shape, p0, p1, width):
    """Polylines for a drag-to-draw shape spanning p0 -> p1."""
    x0, y0 = min(p0[0], p1[0]), min(p0[1], p1[1])
    x1, y1 = max(p0[0], p1[0]), max(p0[1], p1[1])
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if shape == "box":
        return [rect_polyline(p0, p1)]
    if shape == "ellipse":
        return [ellipse_polyline(p0, p1)]
    if shape == "line":
        return [resample([p0, p1])]
    if shape == "arrow":
        return arrow_polylines(p0, p1, width)
    if shape == "darrow":
        return arrow_polylines(p0, p1, width, both=True)
    if shape == "triangle":
        return [polygon_polyline([(cx, y0), (x1, y1), (x0, y1)])]
    if shape == "diamond":
        return [polygon_polyline([(cx, y0), (x1, cy), (cx, y1), (x0, cy)])]
    if shape == "star":
        return [star_polyline(p0, p1)]
    if shape == "heart":
        return [_curve_polyline(p0, p1, _heart)]
    raise ValueError("unknown shape: %s" % shape)


def constrain(shape, p0, p1):
    """Shift-drag: square/circle for box-like shapes, 45-degree steps for lines."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if shape in ("line", "arrow", "darrow"):
        ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
        d = math.hypot(dx, dy)
        return (p0[0] + d * math.cos(ang), p0[1] + d * math.sin(ang))
    side = max(abs(dx), abs(dy))
    return (p0[0] + math.copysign(side, dx or 1), p0[1] + math.copysign(side, dy or 1))


def _seg_dist(p, a, b):
    """Distance from point p to segment a-b."""
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / L2))
    return math.hypot(p[0] - (ax + t * vx), p[1] - (ay + t * vy))


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
# Text rasterisation. Text is rendered by PIL into an RGBA image that both the
# canvas preview and the GIF use, so the two can't disagree about font metrics.
# ---------------------------------------------------------------------------
_TEXT_FONT_PATHS = None


def _font_dirs():
    if IS_WIN:
        return [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""),
                             "Microsoft", "Windows", "Fonts")]
    if IS_MAC:
        return ["/System/Library/Fonts/Supplemental", "/System/Library/Fonts",
                "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts")]


def text_fonts():
    """{font name: file path} for every TEXT_FONTS entry found on this machine."""
    global _TEXT_FONT_PATHS
    if _TEXT_FONT_PATHS is None:
        files = {}
        for d in _font_dirs():
            for dirpath, _dirs, names in os.walk(d):
                for n in names:
                    files.setdefault(n.lower(), os.path.join(dirpath, n))
        _TEXT_FONT_PATHS = {}
        for name, cands in TEXT_FONTS:
            for c in cands:
                if c.lower() in files:
                    _TEXT_FONT_PATHS[name] = files[c.lower()]
                    break
    return _TEXT_FONT_PATHS


def _text_font(name, size):
    key = ("text", name, size)
    if key not in _FONT_CACHE:
        path = text_fonts().get(name)
        try:
            _FONT_CACHE[key] = (ImageFont.truetype(path, size) if path
                                else ImageFont.load_default(size))
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _composite(dst, src, x, y):
    """alpha_composite that tolerates src hanging off any edge of dst (older
    Pillow rejects a negative destination)."""
    x, y = int(x), int(y)
    if x < 0 or y < 0:
        if -x >= src.width or -y >= src.height:
            return
        src = src.crop((max(0, -x), max(0, -y), src.width, src.height))
        x, y = max(0, x), max(0, y)
    if x < dst.width and y < dst.height:
        dst.alpha_composite(src, (x, y))


def _is_light(hexcolor):
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.299 * r + 0.587 * g + 0.114 * b > 150


def render_text(text, font_name, size, color, outline):
    """RGBA image of `text`. outline adds a contrasting edge so the text reads
    on any background."""
    font = _text_font(font_name, size)
    sw = max(1, round(size / 14)) if outline else 0
    spacing = round(size * 0.2)
    bb = ImageDraw.Draw(Image.new("RGBA", (1, 1))).multiline_textbbox(
        (0, 0), text, font=font, spacing=spacing, stroke_width=sw)
    pad = 2
    im = Image.new("RGBA", (max(1, bb[2] - bb[0] + 2 * pad),
                            max(1, bb[3] - bb[1] + 2 * pad)), (0, 0, 0, 0))
    ImageDraw.Draw(im).multiline_text(
        (pad - bb[0], pad - bb[1]), text, font=font, fill=color, spacing=spacing,
        stroke_width=sw, stroke_fill="#000000" if _is_light(color) else "#ffffff")
    return im


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


# Emoji picked with "＋ Any", most-recent first, so they stay in the bar.
EMOJI_RECENT_PATH = os.path.join(os.path.expanduser("~"),
                                 ".snipsquiggle_emoji.json")


def load_custom_emoji():
    try:
        with open(EMOJI_RECENT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, str) and e][:MAX_CUSTOM_EMOJI]
    except Exception:
        return []


def add_custom_emoji(ch):
    """Remember a picked emoji. The built-in EMOJIS are already in the bar."""
    if ch in EMOJIS:
        return load_custom_emoji()
    recent = [ch] + [e for e in load_custom_emoji() if e != ch]
    recent = recent[:MAX_CUSTOM_EMOJI]
    try:
        with open(EMOJI_RECENT_PATH, "w", encoding="utf-8") as f:
            json.dump(recent, f, ensure_ascii=False)
    except Exception:
        pass
    return recent


# ---------------------------------------------------------------------------
# Auto-save. Every snip is written to disk the instant it is taken, before the
# editor is even up, so a capture survives a crash, a stray Esc or a close.
# Annotating produces a second file next to it with a "_2" suffix, rewritten in
# place as you draw - one snip is at most two files, however much you edit.
# ---------------------------------------------------------------------------
SAVE_DIR_NAME = "SnipSquiggle"
ANNOTATED_SUFFIX = "_2"
AUTOSAVE_DEBOUNCE_MS = 700   # quiet period after the last edit before rewriting


def default_save_dir():
    """~/Pictures/SnipSquiggle, or ~/SnipSquiggle where there is no Pictures."""
    home = os.path.expanduser("~")
    pics = os.path.join(home, "Pictures")
    return os.path.join(pics if os.path.isdir(pics) else home, SAVE_DIR_NAME)


def save_snip(image, save_dir):
    """Write the plain snip as a PNG and return its path.

    Names are "snip-<date>-<time>.png". A second snip inside the same second
    gets a "(2)" tail - deliberately not "_2", which is reserved for "the
    annotated version of this snip" (see annotated_path)."""
    os.makedirs(save_dir, exist_ok=True)
    stem = "snip-" + time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(save_dir, stem + ".png")
    dup = 2
    while os.path.exists(path):
        path = os.path.join(save_dir, "%s(%d).png" % (stem, dup))
        dup += 1
    image.save(path, "PNG")
    return path


def annotated_path(snip_path):
    """The companion file for a saved snip. A GIF: annotations are animated."""
    return os.path.splitext(snip_path)[0] + ANNOTATED_SUFFIX + ".gif"


def open_path(path):
    """Open a file (or folder) with the OS default handler."""
    if IS_WIN:
        os.startfile(path)
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_path(path):
    """Show a file in the file manager, selected where the OS supports it."""
    if IS_WIN:
        # Explorer wants the whole thing as one token; a list would be split
        # into separate argv entries and it would just open Documents.
        subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        open_path(os.path.dirname(path) or ".")


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
# Items: everything drawn on the snip. An item is a dict that is never mutated
# once built - moving or restyling one makes a new dict - so the undo history
# and the auto-save thread can hold plain lists of them safely.
#   stroke: {"type": "stroke", "tool", "p0", "p1", "polylines", "color",
#            "width", "anim", "emoji", "seed", "ops"}
#           p0/p1 are the drag corners for shapes (None for the pen), kept so a
#           width change can rebuild the arrow heads at the new size.
#   text:   {"type": "text", "text", "font", "size", "color", "outline",
#            "x", "y", "img", "ops"}       (x, y) is the image's top-left.
# ops are the per-frame draw lists build_ops() makes, plus ("img", x, y, im).
# ---------------------------------------------------------------------------
def make_stroke(tool, polylines, color, width, anim, emoji, seed, p0=None, p1=None):
    return {"type": "stroke", "tool": tool, "p0": p0, "p1": p1,
            "polylines": polylines, "color": color, "width": width,
            "anim": anim, "emoji": emoji, "seed": seed,
            "ops": build_ops(polylines, color, width, anim, emoji, seed)}


def make_text(text, font, size, color, outline, x, y):
    img = render_text(text, font, size, color, outline)
    return {"type": "text", "text": text, "font": font, "size": size,
            "color": color, "outline": outline, "x": x, "y": y, "img": img,
            "ops": [[("img", x, y, img)]] * N_FRAMES}


def restyle(item, **kw):
    """A copy of `item` with some properties changed; keys that don't apply
    to its type (say, a font on a stroke) are ignored."""
    if item["type"] == "text":
        p = {k: kw.get(k, item[k]) for k in ("text", "font", "size", "color", "outline")}
        if all(p[k] == item[k] for k in p):
            return item
        return make_text(p["text"], p["font"], p["size"], p["color"], p["outline"],
                         item["x"], item["y"])
    p = {k: kw.get(k, item[k]) for k in ("color", "width", "anim", "emoji")}
    if all(p[k] == item[k] for k in p):
        return item
    polylines = item["polylines"]
    if item["p0"] is not None and p["width"] != item["width"]:
        polylines = shape_polylines(item["tool"], item["p0"], item["p1"], p["width"])
    return make_stroke(item["tool"], polylines, p["color"], p["width"], p["anim"],
                       p["emoji"], item["seed"], item["p0"], item["p1"])


def _shift_op(op, dx, dy):
    kind = op[0]
    if kind == "line":
        return ("line", [(x + dx, y + dy) for x, y in op[1]], op[2], op[3])
    if kind == "dot":
        return ("dot", op[1] + dx, op[2] + dy, op[3], op[4])
    return (kind, op[1] + dx, op[2] + dy) + tuple(op[3:])   # emoji / img


def translate(item, dx, dy):
    """A copy of `item` moved by (dx, dy). Shifts the cached frames rather than
    rebuilding them, so dragging a long stroke stays smooth."""
    new = dict(item)
    new["ops"] = [[_shift_op(op, dx, dy) for op in fr] for fr in item["ops"]]
    if item["type"] == "text":
        new["x"], new["y"] = item["x"] + dx, item["y"] + dy
    else:
        new["polylines"] = [[(x + dx, y + dy) for x, y in pl] for pl in item["polylines"]]
        if item["p0"] is not None:
            new["p0"] = (item["p0"][0] + dx, item["p0"][1] + dy)
            new["p1"] = (item["p1"][0] + dx, item["p1"][1] + dy)
    return new


def _stroke_pad(item):
    if item["anim"] == "emoji":
        return max(20, item["width"] * 5) * 0.6
    return item["width"] / 2 + JITTER_BASE + item["width"] * 0.35


def item_bbox(item):
    if item["type"] == "text":
        return (item["x"], item["y"], item["x"] + item["img"].width,
                item["y"] + item["img"].height)
    xs = [x for pl in item["polylines"] for x, _ in pl]
    ys = [y for pl in item["polylines"] for _, y in pl]
    pad = _stroke_pad(item)
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def item_hit(item, x, y, slop=5):
    """Is (x, y) on the item? Text is hit anywhere in its box; strokes only
    near the line itself, so you can click through the middle of a box."""
    x0, y0, x1, y1 = item_bbox(item)
    if not (x0 - slop <= x <= x1 + slop and y0 - slop <= y <= y1 + slop):
        return False
    if item["type"] == "text":
        return True
    reach = _stroke_pad(item) + slop
    for pl in item["polylines"]:
        if len(pl) == 1 and _dist(pl[0], (x, y)) <= reach:
            return True
        for a, b in zip(pl, pl[1:]):
            if _seg_dist((x, y), a, b) <= reach:
                return True
    return False


def open_system_emoji_picker():
    """Open the OS emoji panel over whatever has keyboard focus. Best effort:
    Windows 10+ has Win+. ; macOS has the Character Viewer; Linux has no
    standard one, so there the user types or pastes."""
    try:
        if IS_WIN:
            u32 = ctypes.windll.user32
            VK_LWIN, VK_PERIOD, KEYUP = 0x5B, 0xBE, 0x0002
            u32.keybd_event(VK_LWIN, 0, 0, 0)
            u32.keybd_event(VK_PERIOD, 0, 0, 0)
            u32.keybd_event(VK_PERIOD, 0, KEYUP, 0)
            u32.keybd_event(VK_LWIN, 0, KEYUP, 0)
            return True
        if IS_MAC:
            from AppKit import NSApp
            NSApp.orderFrontCharacterPalette_(None)
            return True
    except Exception as ex:
        _log("couldn't open the emoji picker: %s" % ex)
    return False


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
                 static_copied=False, saved_path=None, save_error=None):
        self.root = root
        self.image = image.convert("RGB")
        self.new_snip_cb = new_snip_cb
        self.on_close = on_close or root.quit

        self.color = "#ff3b30"
        self.width = 5
        self.tool = "pen"          # move | pen | text | one of SHAPES
        self.shape = "box"         # last shape picked, behind the Shape button
        self.anim = "squiggle"     # squiggle | ants | dots | emoji
        self.emoji = "🔥"
        self.custom_emoji = load_custom_emoji()
        fonts = text_fonts()
        self.font = (DEFAULT_TEXT_FONT if DEFAULT_TEXT_FONT in fonts
                     else next(iter(fonts), "Default"))
        self.text_size = 32
        self.text_outline = True
        self.items = []            # see make_stroke / make_text
        self._history = []         # earlier versions of self.items, for undo
        self.sel = None            # index of the selected item, if any
        self._drag = None          # item being moved: {idx, orig, start, ...}
        self._text_edit = None     # the open text box: {widget, win, x, y, idx}
        self._emoji_pop = None     # the "any emoji" window, while open
        self.frame = 0
        self._seed = 0
        self._live_pts = []
        self._live_start = None
        self._emoji_photos = {}
        self._img_photos = {}      # id(PIL image) -> (image, PhotoImage)
        self._swatch_imgs = {}     # color -> menu swatch PhotoImage
        self._menu_vars = []       # keeps context-menu radio variables alive
        self.watermark = None      # see load_watermark() for shape
        self._wm_drag = None       # (dx, dy) offset while dragging the logo
        self._closed = False
        self._tick_id = None

        # Auto-save state. saved_path is the PNG the controller already wrote;
        # anno_saved is its "_2" GIF, which only exists once something is drawn.
        self.saved_path = saved_path
        self.save_error = save_error
        self.anno_saved = None
        self._save_after_id = None   # pending debounced rewrite
        self._save_thread = None     # the rewrite currently in flight, if any
        self._save_q = queue.Queue()  # (path, error) back from that thread
        self._resave = False         # edited again mid-write; go round again

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
        self.canvas.bind("<Double-Button-1>", self._double)
        self.canvas.bind("<Motion>", self._hover)

        # Right-click menu. macOS reports the right button as 2, and Ctrl+click
        # is the one-button-mouse equivalent there.
        if IS_MAC:
            self.canvas.bind("<Button-2>", self._context)
            self.canvas.bind("<Control-Button-1>", self._context)
        else:
            self.canvas.bind("<Button-3>", self._context)

        # Mouse wheel resizes the watermark when hovering over it.
        self.canvas.bind("<MouseWheel>", self._on_wheel)                 # Win/Mac
        self.canvas.bind("<Button-4>", lambda e: self._on_wheel(e, 1))   # Linux
        self.canvas.bind("<Button-5>", lambda e: self._on_wheel(e, -1))  # Linux

        # Ctrl on Win/Linux, Command on macOS. None of these fire while a text
        # box is open: there Ctrl+C / Ctrl+Z belong to the text being typed.
        mod = "Command" if IS_MAC else "Control"
        self.win.bind(f"<{mod}-c>", self._key(self.copy_gif))
        self.win.bind(f"<{mod}-s>", self._key(self.save_gif))
        self.win.bind(f"<{mod}-z>", self._key(self.undo))
        self.win.bind(f"<{mod}-n>", self._key(self.new_snip))
        self.win.bind(f"<{mod}-d>", self._key(self.duplicate_selected))
        self.win.bind("<Escape>", self._key(self._escape))
        self.win.bind("<Key>", self._on_key)

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

    def _link(self, parent, text, cmd, fg=LINK_FG):
        """A clickable file name in the header."""
        lk = tk.Label(parent, text=text, bg="#1e1e1e", fg=fg, cursor="hand2",
                      font=(UI_FONT[0], UI_FONT[1], "underline"))
        lk._basefg = fg
        lk.bind("<Button-1>", lambda e: cmd())
        lk.bind("<Enter>", lambda e: lk.configure(fg="#ffffff"))
        lk.bind("<Leave>", lambda e: lk.configure(fg=lk._basefg))
        lk.pack(side="left", padx=(2, 6))
        return lk

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
        for name, label in (("move", "↖ Move"), ("pen", "✎ Pen"), ("text", "T Text")):
            self.tool_btns[name] = self._btn(bar, label, lambda n=name: self.set_tool(n))
        # The shape button draws the last shape picked; ▾ picks another.
        self.shape_btn = self._btn(bar, SHAPE_LABELS[self.shape],
                                   lambda: self.set_tool(self.shape))
        self.shape_btn.pack_configure(padx=(2, 0))
        self.shape_menu_btn = self._btn(bar, "▾", self._shape_menu, padx=4)
        self.shape_menu_btn.pack_configure(padx=(0, 2))
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
        self.width_btns = {}
        for w, lbl in WIDTHS:
            self.width_btns[w] = self._btn(bar, lbl, lambda ww=w: self.set_width(ww))
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
        for name, label in ANIMS:
            self.anim_btns[name] = self._btn(bar2, label, lambda n=name: self.set_anim(n))
        self._sep(bar2)
        self.emoji_btns = {}
        for ch in EMOJIS:
            self.emoji_btns[ch] = self._btn(bar2, ch,
                                            lambda c=ch: self.set_emoji(c),
                                            font=EMOJI_UI_FONT, padx=5, pady=2)
        # Emoji picked with "＋ Any" live in their own frame so the list can be
        # rebuilt as new ones are picked.
        self.custom_emoji_bar = tk.Frame(bar2, bg="#1e1e1e")
        self.custom_emoji_bar.pack(side="left")
        self.custom_emoji_btns = {}
        self._btn(bar2, "＋ Any", self.pick_any_emoji)

        bar_text = tk.Frame(self.win, bg="#1e1e1e")
        bar_text.pack(fill="x", padx=10, pady=(0, 6))
        self._label(bar_text, "Text:")
        self.font_btn = self._btn(bar_text, "", self._font_menu)
        self.size_btn = self._btn(bar_text, "", self._size_menu)
        self.outline_btn = self._btn(bar_text, "◌ Outline", self.toggle_outline)
        self._label(bar_text, "right-click for options · ↖ Move drags items "
                              "(Del removes) · Shift = straight / square")

        self.status = tk.Label(bar_text, text="", bg="#1e1e1e", fg="#4cd964",
                               font=UI_FONT)
        self.status.pack(side="right", padx=(6, 2))

        bar3 = tk.Frame(self.win, bg="#1e1e1e")
        bar3.pack(fill="x", padx=10, pady=(0, 8))
        self._label(bar3, "💾 Saved:")
        self.snip_link = self._link(bar3, "", self._open_saved)
        self.anno_sep = tk.Label(bar3, text="", bg="#1e1e1e", fg="#5a5a5a",
                                 font=UI_FONT)
        self.anno_sep.pack(side="left")
        self.anno_link = self._link(bar3, "", self._open_annotated)
        self.folder_link = self._link(bar3, "", self._open_folder)
        self.save_status = tk.Label(bar3, text="", bg="#1e1e1e", fg="#9a9a9a",
                                    font=UI_FONT)
        self.save_status.pack(side="right", padx=(6, 2))

        self._rebuild_custom_emoji()
        self._refresh_btns()
        self._refresh_links()
        if self.save_error:
            self.save_status.configure(text="⚠ auto-save failed: %s"
                                            % self.save_error, fg="#ff6b6b")

    def _rebuild_custom_emoji(self):
        for w in self.custom_emoji_bar.winfo_children():
            w.destroy()
        self.custom_emoji_btns = {}
        for ch in self.custom_emoji:
            self.custom_emoji_btns[ch] = self._btn(
                self.custom_emoji_bar, ch, lambda c=ch: self.set_emoji(c),
                font=EMOJI_UI_FONT, padx=5, pady=2)

    def _current(self):
        """The style the toolbar shows: the selected item's, else the defaults
        the next item will be drawn with."""
        cur = {"color": self.color, "width": self.width, "anim": self.anim,
               "emoji": self.emoji, "font": self.font, "size": self.text_size,
               "outline": self.text_outline}
        item = self._selected()
        if item:
            cur.update({k: item[k] for k in cur if k in item})
        return cur

    def _refresh_btns(self):
        cur = self._current()
        for name, b in self.tool_btns.items():
            self._set_sel(b, name == self.tool)
        self.shape_btn.configure(text=SHAPE_LABELS[self.shape])
        self._set_sel(self.shape_btn, self.tool in SHAPE_LABELS)
        for w, b in self.width_btns.items():
            self._set_sel(b, w == cur["width"])
        for name, b in self.anim_btns.items():
            self._set_sel(b, name == cur["anim"])
        for ch, b in list(self.emoji_btns.items()) + list(self.custom_emoji_btns.items()):
            self._set_sel(b, cur["anim"] == "emoji" and ch == cur["emoji"])
        for c, sw in getattr(self, "swatches", {}).items():
            sel = (c == cur["color"])
            sw.configure(highlightbackground="#ffffff" if sel else "#555",
                         highlightthickness=2 if sel else 1)
        self.font_btn.configure(text="Aa %s ▾" % cur["font"])
        self.size_btn.configure(text="%dpx ▾" % cur["size"])
        self._set_sel(self.outline_btn, cur["outline"])

    def _refresh_links(self):
        """Header line: where this snip landed, and its annotated companion."""
        if not self.saved_path:
            self.snip_link._basefg = "#7a7a7a"
            self.snip_link.configure(text="failed" if self.save_error else "off",
                                     fg="#7a7a7a", cursor="arrow")
            for w in (self.folder_link, self.anno_sep, self.anno_link):
                w.configure(text="")
            return
        self.snip_link.configure(text=os.path.basename(self.saved_path))
        self.folder_link.configure(text="📂 folder")
        if self.anno_saved:
            self.anno_sep.configure(text="·")
            self.anno_link.configure(text="✎ "
                                          + os.path.basename(self.anno_saved))
        else:
            self.anno_sep.configure(text="")
            self.anno_link.configure(text="")

    def _open(self, path, reveal=False):
        if not path or not os.path.exists(path):
            return
        try:
            (reveal_path if reveal else open_path)(path)
        except Exception as ex:
            messagebox.showerror("Couldn't open", str(ex), parent=self.win)

    def _open_saved(self):
        self._open(self.saved_path)

    def _open_annotated(self):
        self._open(self.anno_saved)

    def _open_folder(self):
        self._open(self.anno_saved or self.saved_path, reveal=True)

    # -- tool + style setters ------------------------------------------------
    # Every style setter changes the default for the next item AND restyles the
    # selected item, so the toolbar and the right-click menu both edit
    # whatever is selected.
    def set_tool(self, name):
        self._commit_text()
        self.tool = name
        if name in SHAPE_LABELS:
            self.shape = name
        if name != "move":
            self.sel = None
        self.canvas.configure(cursor={"move": "arrow", "text": "xterm"}.get(name, "pencil"))
        self._refresh_btns()

    def set_anim(self, name):
        self.anim = name
        self._style(anim=name)

    def set_emoji(self, ch):
        self.emoji = ch
        self.anim = "emoji"
        self._style(anim="emoji", emoji=ch)

    def set_color(self, c):
        self.color = c
        self._style(color=c)

    def pick_color(self):
        c = colorchooser.askcolor(color=self._current()["color"], parent=self.win)[1]
        if c:
            self.set_color(c)

    def set_width(self, w):
        self.width = w
        self._style(width=w)

    def set_font(self, name):
        self.font = name
        self._style(font=name)

    def set_text_size(self, size):
        self.text_size = size
        self._style(size=size)

    def toggle_outline(self):
        self.text_outline = not self._current()["outline"]
        self._style(outline=self.text_outline)

    def _style(self, **kw):
        item = self._selected()
        if item:
            new = restyle(item, **kw)
            if new is not item:
                self._push()
                self.items[self.sel] = new
                self._mark_dirty()
        if self._text_edit:
            self._style_text_box()
        self._refresh_btns()

    def _popup(self, menu):
        try:
            menu.tk_popup(self.win.winfo_pointerx(), self.win.winfo_pointery())
        finally:
            menu.grab_release()

    def _radio_menu(self, parent, choices, current, command):
        """A menu of radio entries [(value, label)], `current` ticked."""
        menu = tk.Menu(parent, tearoff=0)
        var = tk.StringVar(value=str(current))
        self._menu_vars.append(var)
        for value, label in choices:
            menu.add_radiobutton(label=label, value=str(value), variable=var,
                                 command=lambda v=value: command(v))
        return menu

    def _font_choices(self):
        return [(n, n) for n, _ in TEXT_FONTS if n in text_fonts()] or [("Default", "Default")]

    def _shape_menu(self):
        self._menu_vars = []
        self._popup(self._radio_menu(self.win, SHAPES, self.tool, self.set_tool))

    def _font_menu(self):
        self._menu_vars = []
        self._popup(self._radio_menu(self.win, self._font_choices(),
                                     self._current()["font"], self.set_font))

    def _size_menu(self):
        self._menu_vars = []
        self._popup(self._radio_menu(self.win, [(s, "%d px" % s) for s in TEXT_SIZES],
                                     self._current()["size"], self.set_text_size))

    # -- any emoji ------------------------------------------------------------
    def pick_any_emoji(self):
        """A small box with the OS emoji picker opened over it. Picking an
        emoji there uses it straight away; or type / paste any emoji or short
        text (like the built-in "FFS") and press Enter."""
        if self._emoji_pop is not None:
            self._emoji_pop.lift()
            return
        top = tk.Toplevel(self.win)
        self._emoji_pop = top
        top.title("Any emoji")
        top.configure(bg="#1e1e1e")
        top.transient(self.win)
        top.resizable(False, False)
        top.geometry("+%d+%d" % (self.win.winfo_pointerx() - 160,
                                 self.win.winfo_pointery() + 20))
        where = ("the emoji panel (Win + .)" if IS_WIN else
                 "the Character Viewer (Ctrl+Cmd+Space)" if IS_MAC else None)
        hint = (("Pick one from %s,\nor type / paste any emoji or text, then Enter."
                 % where) if where else
                "Type or paste any emoji or short text, then press Enter.")
        tk.Label(top, text=hint, bg="#1e1e1e", fg="#cfcfcf", font=UI_FONT,
                 justify="center").pack(padx=14, pady=(12, 6))
        var = tk.StringVar()
        entry = tk.Entry(top, textvariable=var, font=(EMOJI_UI_FONT[0], 22),
                         width=8, justify="center", bg="#2d2d2d", fg="white",
                         insertbackground="white", relief="flat")
        entry.pack(padx=14, pady=4)
        row = tk.Frame(top, bg="#1e1e1e")
        row.pack(pady=(6, 12))
        pending = [None]

        def close(_e=None):
            self._emoji_pop = None
            top.destroy()

        def use(_e=None):
            s = var.get().strip()[:16]
            if not s or self._emoji_pop is not top:
                return
            close()
            self.custom_emoji = add_custom_emoji(s)
            self._rebuild_custom_emoji()
            self.set_emoji(s)
            self.win.focus_force()

        def changed(*_a):
            # The picker inserts a whole emoji at once (incl. any skin-tone /
            # ZWJ parts); wait a beat so they've all landed, then use it.
            # Plain typing (no emoji) waits for Enter instead.
            if pending[0] is not None:
                top.after_cancel(pending[0])
                pending[0] = None
            if any(ord(c) > 0x2000 for c in var.get()):
                pending[0] = top.after(400, use)

        var.trace_add("write", changed)
        self._btn(row, "Use", use, base=BTN_SEL)
        self._btn(row, "Cancel", close)
        entry.bind("<Return>", use)
        top.bind("<Escape>", close)
        top.protocol("WM_DELETE_WINDOW", close)

        def focus_and_open():
            if self._emoji_pop is top:
                top.focus_force()
                entry.focus_set()
                top.after(150, open_system_emoji_picker)
        top.after(120, focus_and_open)

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
        self._mark_dirty()

    def remove_watermark(self):
        self.watermark = None
        self._wm_drag = None
        self._mark_dirty()

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
        self._mark_dirty()

    # -- items: hit-testing, selection, undo ----------------------------------
    def _selected(self):
        if self.sel is not None and 0 <= self.sel < len(self.items):
            return self.items[self.sel]
        return None

    def _hit(self, x, y, kind=None):
        """Index of the topmost item under (x, y), optionally of one type."""
        for i in range(len(self.items) - 1, -1, -1):
            it = self.items[i]
            if (kind is None or it["type"] == kind) and item_hit(it, x, y):
                return i
        return None

    def _push(self, snapshot=None):
        """Record the drawing as it is now (or `snapshot`) for undo."""
        self._history.append(list(self.items) if snapshot is None else snapshot)
        del self._history[:-100]

    def undo(self):
        if self._text_edit:          # undo while typing = drop the text box
            self._close_text()
            return
        if self._history:
            self.items = self._history.pop()
            self.sel = None
            self._refresh_btns()
            self._mark_dirty()

    def clear(self):
        self._commit_text()
        if self.items:
            self._push()
            self.items = []
            self.sel = None
            self._refresh_btns()
            self._mark_dirty()

    def _edit_selected(self, fn):
        """Apply fn(items, idx) -> new selected index to the selection."""
        if self._selected() is None:
            return
        self._push()
        self.sel = fn(self.items, self.sel)
        self._refresh_btns()
        self._mark_dirty()

    def delete_selected(self):
        def delete(its, i):
            del its[i]
            return None
        self._edit_selected(delete)

    def duplicate_selected(self):
        def dup(its, i):
            its.append(translate(its[i], 16, 16))
            return len(its) - 1
        self._edit_selected(dup)

    def bring_to_front(self):
        def front(its, i):
            its.append(its.pop(i))
            return len(its) - 1
        self._edit_selected(front)

    def send_to_back(self):
        def back(its, i):
            its.insert(0, its.pop(i))
            return 0
        self._edit_selected(back)

    def nudge(self, dx, dy):
        def move(its, i):
            its[i] = translate(its[i], dx, dy)
            return i
        self._edit_selected(move)

    # -- keyboard ---------------------------------------------------------------
    def _key(self, fn):
        """A shortcut handler that stands aside while a text box is open."""
        def handler(_e):
            if not self._text_edit:
                fn()
        return handler

    def _escape(self):
        if self.sel is not None:
            self.sel = None
            self._refresh_btns()
        else:
            self._quit()

    def _on_key(self, e):
        if self._text_edit or self._emoji_pop is not None:
            return
        if e.state & (0x4 | (0x8 if IS_MAC else 0)):    # Ctrl / Cmd combos
            return
        ks = e.keysym
        if ks in ("Delete", "BackSpace"):
            self.delete_selected()
        elif ks in ("Left", "Right", "Up", "Down"):
            step = 10 if e.state & 0x1 else 1
            dx = {"Left": -step, "Right": step}.get(ks, 0)
            dy = {"Up": -step, "Down": step}.get(ks, 0)
            self.nudge(dx, dy)
        elif e.char and e.char.lower() in TOOL_KEYS:
            self.set_tool(TOOL_KEYS[e.char.lower()])

    # -- mouse --------------------------------------------------------------
    def _down(self, e):
        if self._text_edit:          # clicking off an open text box commits it
            self._commit_text()
            return
        if self.tool == "move":
            idx = self._hit(e.x, e.y)
            if idx is not None:
                self.sel = idx
                self._drag = {"idx": idx, "orig": self.items[idx], "start": (e.x, e.y),
                              "snap": list(self.items), "moved": False}
                self._refresh_btns()
                return
        elif self.sel is not None:
            self.sel = None
            self._refresh_btns()
        # Clicking on the logo grabs it for repositioning instead of drawing.
        if self._in_watermark(e.x, e.y):
            wm = self.watermark
            self._wm_drag = (e.x - wm["cx"], e.y - wm["cy"])
            return
        if self.tool == "move":
            if self.sel is not None:
                self.sel = None
                self._refresh_btns()
            return
        if self.tool == "text":
            idx = self._hit(e.x, e.y, kind="text")
            if idx is not None:
                self._open_text(idx=idx)
            else:
                self._open_text(x=e.x, y=e.y)
            return
        self._live_start = (e.x, e.y)
        self._live_pts = [(e.x, e.y)]

    def _shape_end(self, e):
        p = (e.x, e.y)
        if e.state & 0x1:            # Shift: square / circle / 45-degree line
            p = constrain(self.tool, self._live_start, p)
        return p

    def _move(self, e):
        if self._wm_drag is not None:
            wm = self.watermark
            dx, dy = self._wm_drag
            wm["cx"] = min(max(e.x - dx, wm["w"] / 2), self.image.width - wm["w"] / 2)
            wm["cy"] = min(max(e.y - dy, wm["h"] / 2), self.image.height - wm["h"] / 2)
            return
        d = self._drag
        if d is not None:
            dx, dy = e.x - d["start"][0], e.y - d["start"][1]
            if not d["moved"] and abs(dx) + abs(dy) < 3:
                return               # a click, not a drag - don't nudge it
            d["moved"] = True
            self.items[d["idx"]] = translate(d["orig"], dx, dy)
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
            return
        end = self._shape_end(e)
        if _dist(self._live_start, end) < 2:
            return
        for pl in shape_polylines(self.tool, self._live_start, end, self.width):
            if len(pl) >= 2:
                self.canvas.create_line(*sum(pl, ()), fill=self.color,
                                        width=self.width, capstyle="round",
                                        joinstyle="round", tags="live")

    def _up(self, e):
        if self._wm_drag is not None:
            self._wm_drag = None
            self._mark_dirty()
            return
        d = self._drag
        if d is not None:
            self._drag = None
            if d["moved"]:
                self._push(d["snap"])
                self._mark_dirty()
            return
        if self._live_start is None:
            return
        self.canvas.delete("live")
        start = self._live_start
        end = (e.x, e.y) if self.tool == "pen" else self._shape_end(e)
        self._live_start = None

        if self.tool == "pen":
            if len(self._live_pts) < 2:
                return
            polylines, p0, p1 = [resample(self._live_pts)], None, None
        else:
            if _dist(start, end) < 5:
                return
            polylines = shape_polylines(self.tool, start, end, self.width)
            p0, p1 = start, end

        self._seed += 1
        self._push()
        self.items.append(make_stroke(self.tool, polylines, self.color, self.width,
                                      self.anim, self.emoji, self._seed, p0, p1))
        self._mark_dirty()

    def _double(self, e):
        """Double-click a text item (any tool) to edit it."""
        if self._text_edit:
            return
        idx = self._hit(e.x, e.y, kind="text")
        if idx is not None:
            self._open_text(idx=idx)

    def _hover(self, e):
        if self.tool == "move" and self._drag is None:
            over = self._hit(e.x, e.y) is not None or self._in_watermark(e.x, e.y)
            cur = "fleur" if over else "arrow"
            if self.canvas.cget("cursor") != cur:
                self.canvas.configure(cursor=cur)

    def _context(self, e):
        """Right-click: item actions if there's an item under the pointer, then
        quick picks for tool, shape and style. Style picks restyle the item."""
        self._commit_text()
        self.sel = self._hit(e.x, e.y)
        self._refresh_btns()
        self._menu_vars = []
        cur = self._current()
        item = self._selected()
        m = tk.Menu(self.win, tearoff=0)
        mod = "Cmd" if IS_MAC else "Ctrl"

        if item:
            if item["type"] == "text":
                m.add_command(label="Edit text…", command=lambda: self._open_text(idx=self.sel))
            m.add_command(label="Duplicate", accelerator=mod + "+D",
                          command=self.duplicate_selected)
            m.add_command(label="Bring to front", command=self.bring_to_front)
            m.add_command(label="Send to back", command=self.send_to_back)
            m.add_command(label="Delete", accelerator="Del", command=self.delete_selected)
            m.add_separator()

        tools = [("move", "↖ Move"), ("pen", "✎ Pen"), ("text", "T Text")]
        tool_var = tk.StringVar(value=self.tool)
        self._menu_vars.append(tool_var)
        for name, label in tools:
            m.add_radiobutton(label=label, value=name, variable=tool_var,
                              accelerator=next(k.upper() for k, v in TOOL_KEYS.items() if v == name),
                              command=lambda n=name: self.set_tool(n))
        m.add_cascade(label="Shape", menu=self._radio_menu(m, SHAPES, self.tool, self.set_tool))
        m.add_separator()

        colors = tk.Menu(m, tearoff=0)
        for c in PALETTE + ([cur["color"]] if cur["color"] not in PALETTE else []):
            colors.add_command(label=("✓ " if c == cur["color"] else "   ") + c,
                               image=self._swatch(c), compound="left",
                               command=lambda cc=c: self.set_color(cc))
        colors.add_separator()
        colors.add_command(label="Custom…", command=self.pick_color)
        m.add_cascade(label="Colour", menu=colors)
        if not item or item["type"] == "stroke":
            m.add_cascade(label="Width", menu=self._radio_menu(
                m, WIDTHS, cur["width"], self.set_width))
            m.add_cascade(label="Animation", menu=self._radio_menu(
                m, ANIMS, cur["anim"], self.set_anim))
            emo = tk.Menu(m, tearoff=0)
            for i, ch in enumerate(EMOJIS + [c for c in self.custom_emoji if c not in EMOJIS]):
                emo.add_command(label=ch, image=self._emoji_photo(ch, 20),
                                columnbreak=(i > 0 and i % 7 == 0),
                                command=lambda c=ch: self.set_emoji(c))
            emo.add_separator()
            emo.add_command(label="Any emoji…", command=self.pick_any_emoji)
            m.add_cascade(label="Emoji", menu=emo)
        if not item or item["type"] == "text":
            m.add_cascade(label="Font", menu=self._radio_menu(
                m, self._font_choices(), cur["font"], self.set_font))
            m.add_cascade(label="Text size", menu=self._radio_menu(
                m, [(s, "%d px" % s) for s in TEXT_SIZES], cur["size"], self.set_text_size))
            outline = tk.BooleanVar(value=cur["outline"])
            self._menu_vars.append(outline)
            m.add_checkbutton(label="Text outline", variable=outline,
                              command=self.toggle_outline)
        m.add_separator()
        m.add_command(label="Undo", accelerator=mod + "+Z", command=self.undo,
                      state="normal" if self._history else "disabled")
        m.add_command(label="Clear all", command=self.clear,
                      state="normal" if self.items else "disabled")
        m.add_command(label="Copy GIF", accelerator=mod + "+C", command=self.copy_gif)
        self._popup(m)

    def _swatch(self, color):
        if color not in self._swatch_imgs:
            img = tk.PhotoImage(width=14, height=14)
            img.put("#777777", to=(0, 0, 14, 14))
            img.put(color, to=(1, 1, 13, 13))
            self._swatch_imgs[color] = img
        return self._swatch_imgs[color]

    # -- text boxes ---------------------------------------------------------
    def _open_text(self, x=None, y=None, idx=None):
        """Open an in-place text box: a new one at (x, y), or over item `idx`
        to edit it. Enter commits, Shift+Enter is a new line, Esc cancels."""
        self._commit_text()
        initial = ""
        if idx is not None:
            item = self.items[idx]
            x, y, initial = item["x"], item["y"], item["text"]
            # Edit with the item's own style, so the toolbar shows it and
            # committing doesn't silently restyle it.
            self.color, self.font = item["color"], item["font"]
            self.text_size, self.text_outline = item["size"], item["outline"]
        self.sel = None
        t = tk.Text(self.canvas, height=1, width=4, bd=0, wrap="none", undo=True,
                    highlightthickness=1, highlightbackground=SEL_COLOR,
                    highlightcolor=SEL_COLOR, padx=2, pady=0)
        t.insert("1.0", initial)
        win = self.canvas.create_window(x, y, anchor="nw", window=t)
        self._text_edit = {"widget": t, "win": win, "x": x, "y": y, "idx": idx}
        self._style_text_box()
        t.bind("<Return>", lambda e: (self._commit_text(), "break")[1])
        t.bind("<KP_Enter>", lambda e: (self._commit_text(), "break")[1])
        t.bind("<Shift-Return>", lambda e: (t.insert("insert", "\n"),
                                            self._fit_text_box(), "break")[2])
        t.bind("<Escape>", lambda e: (self._close_text(), "break")[1])
        t.bind("<KeyRelease>", lambda e: self._fit_text_box())
        t.focus_set()
        t.mark_set("insert", "end")
        self._refresh_btns()

    def _style_text_box(self):
        te = self._text_edit
        if not te:
            return
        family = self.font.replace(" Bold", "")
        weight = "bold" if self.font.endswith("Bold") else "normal"
        bg = "#1e1e1e" if _is_light(self.color) else "#f2f2f2"
        te["widget"].configure(font=(family, -self.text_size, weight), fg=self.color,
                               bg=bg, insertbackground=self.color)
        self._fit_text_box()

    def _fit_text_box(self):
        te = self._text_edit
        if not te:
            return
        lines = te["widget"].get("1.0", "end-1c").split("\n")
        te["widget"].configure(width=max(4, max(len(ln) for ln in lines) + 2),
                               height=len(lines))

    def _close_text(self):
        """Remove the text box; returns what was typed (None if none open)."""
        te = self._text_edit
        if not te:
            return None
        text = te["widget"].get("1.0", "end-1c").rstrip()
        self._text_edit = None
        self.canvas.delete(te["win"])
        te["widget"].destroy()
        if not self._closed:
            self.win.focus_set()      # so shortcuts work again
        return text, te

    def _commit_text(self):
        closed = self._close_text()
        if not closed:
            return
        text, te = closed
        idx = te["idx"]
        if idx is not None and idx >= len(self.items):
            idx = None               # undone while it was open; add it as new
        if not text.strip():
            if idx is not None:      # emptied an existing text: delete it
                self._push()
                del self.items[idx]
                self._mark_dirty()
            return
        new = make_text(text, self.font, self.text_size, self.color,
                        self.text_outline, te["x"], te["y"])
        if idx is not None:
            old = self.items[idx]
            if all(old[k] == new[k] for k in ("text", "font", "size", "color", "outline")):
                return
            self._push()
            self.items[idx] = new
        else:
            self._push()
            self.items.append(new)
        self._mark_dirty()

    # -- canvas animation loop ---------------------------------------------
    def _emoji_photo(self, char, px):
        key = (char, int(px))
        if key not in self._emoji_photos:
            self._emoji_photos[key] = ImageTk.PhotoImage(emoji_image(char, int(px)))
        return self._emoji_photos[key]

    def _img_photo(self, im):
        hit = self._img_photos.get(id(im))
        if hit is None or hit[0] is not im:
            hit = (im, ImageTk.PhotoImage(im))
            self._img_photos[id(im)] = hit
        return hit[1]

    def _tick(self):
        if self._closed:
            return
        self._drain_saves()
        self.frame = (self.frame + 1) % N_FRAMES
        self.canvas.delete("stroke")
        if self.watermark:
            wm = self.watermark
            self.canvas.create_image(wm["cx"], wm["cy"],
                                     image=wm["photos"][self.frame], tags="stroke")
        editing = self._text_edit["idx"] if self._text_edit else None
        for i, s in enumerate(self.items):
            if i == editing:         # the open text box stands in for it
                continue
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
                elif kind == "img":
                    _, x, y, im = op
                    self.canvas.create_image(x, y, anchor="nw",
                                             image=self._img_photo(im), tags="stroke")
        item = self._selected()
        if item:
            x0, y0, x1, y1 = item_bbox(item)
            for col, dash in (("#ffffff", ()), (SEL_COLOR, (5, 3))):
                self.canvas.create_rectangle(x0 - 3, y0 - 3, x1 + 3, y1 + 3,
                                             outline=col, width=1, dash=dash,
                                             tags="stroke")
        self.canvas.tag_raise("live")
        # Drop photos of text that is gone (edited, restyled or undone away).
        if len(self._img_photos) > 2 * len(self.items) + 16:
            live = {id(s["img"]) for s in self.items if s["type"] == "text"}
            self._img_photos = {k: v for k, v in self._img_photos.items() if k in live}
        self._tick_id = self.win.after(FRAME_MS, self._tick)

    # -- gif rendering ------------------------------------------------------
    def _render_frames(self, items=None, wm=None):
        """Composite the snip + annotations into N_FRAMES full-size images.

        The auto-save thread passes its own snapshot of the items and the
        watermark, so a render in progress can't trip over the item the user
        is drawing or dragging meanwhile. Tk-thread callers use the live state."""
        items = self.items if items is None else items
        wm = self._wm_snapshot() if wm is None else wm
        frames = []
        for f in range(N_FRAMES):
            im = self.image.convert("RGBA")
            if wm:
                wframes, wcx, wcy = wm
                g = wframes[f]
                _composite(im, g, wcx - g.width / 2, wcy - g.height / 2)
            d = ImageDraw.Draw(im)
            for s in items:
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
                        _composite(im, g, x - g.width / 2, y - g.height / 2)
                    elif kind == "img":
                        _, x, y, g = op
                        _composite(im, g, x, y)
            frames.append(im.convert("RGB"))
        return frames

    def _write_gif(self, path, items=None, wm=None):
        frames = self._render_frames(items, wm)
        pframes = [fr.convert("P", palette=Image.ADAPTIVE, colors=256) for fr in frames]
        # format is explicit: the auto-save renders to a ".part" temp first,
        # and PIL would otherwise guess the format from that extension.
        pframes[0].save(path, format="GIF", save_all=True,
                        append_images=pframes[1:], loop=0, duration=FRAME_MS,
                        disposal=2, optimize=True)
        return frames[0]

    def copy_gif(self):
        self._commit_text()
        path = os.path.join(tempfile.gettempdir(), "SnipSquiggle.gif")
        try:
            static = self._write_gif(path)
            set_clipboard(path, static)
        except Exception as ex:
            messagebox.showerror("Copy failed", str(ex), parent=self.win)
            return
        self._flash(self.copy_btn, "✓ Copied!")

    def save_gif(self):
        self._commit_text()
        # Start where the auto-saves live, under the same name, so an explicit
        # save is "keep this one" rather than "hunt for a folder".
        anno = annotated_path(self.saved_path) if self.saved_path else ""
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".gif",
                filetypes=[("GIF", "*.gif")],
                initialdir=os.path.dirname(anno) or None,
                initialfile=os.path.basename(anno) or "snip.gif")
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

    # -- auto-save of the annotated copy ------------------------------------
    def _wm_snapshot(self):
        """(frames, cx, cy) - a view of the watermark safe to render off-thread."""
        wm = self.watermark
        if not wm or "frames_img" not in wm:
            return None
        return (wm["frames_img"], wm["cx"], wm["cy"])

    def _mark_dirty(self):
        """The drawing changed: queue a debounced rewrite of the "_2" file.

        Debounced because an 8-frame GIF of a full-screen snip takes long
        enough to notice, and every pen stroke lands here."""
        if not self.saved_path or self._closed:
            return
        if self._save_after_id is not None:
            try:
                self.win.after_cancel(self._save_after_id)
            except Exception:
                pass
        self._save_after_id = self.win.after(AUTOSAVE_DEBOUNCE_MS, self._autosave)

    def _autosave(self):
        self._save_after_id = None
        if self._closed or not self.saved_path:
            return
        if self._saving():          # a write is in flight; go again once it lands
            self._resave = True
            return
        if not self.items and not self.watermark:
            self._drop_annotated()
            return
        items, wm = list(self.items), self._wm_snapshot()
        self.save_status.configure(text="saving…", fg="#9a9a9a")
        self._save_thread = threading.Thread(target=self._autosave_worker,
                                             args=(items, wm), daemon=True)
        self._save_thread.start()

    def _saving(self):
        t = self._save_thread
        return t is not None and t.is_alive()

    def _autosave_worker(self, items, wm):
        """Render + write off the Tk thread, and hand the result back through a
        queue - _tick drains it. Tcl is not thread-safe, so nothing here may
        touch a widget."""
        try:
            self._save_q.put((self._write_annotated(items, wm), None))
        except Exception as ex:
            self._save_q.put((None, str(ex)))

    def _write_annotated(self, items, wm):
        """Write the "_2" GIF and return its path.

        Rendered to a sibling temp file and swapped in, so nobody ever opens a
        half-written GIF and a failed render can't destroy the last good one."""
        path = annotated_path(self.saved_path)
        tmp = path + ".part"
        try:
            self._write_gif(tmp, items, wm)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except Exception:
                pass
            raise
        return path

    def _drain_saves(self):
        """Pick up finished writes (called from _tick, on the Tk thread)."""
        try:
            while True:
                path, err = self._save_q.get_nowait()
                if err:
                    self.save_status.configure(text="⚠ save failed: " + err,
                                               fg="#ff6b6b")
                else:
                    self.anno_saved = path
                    self.save_status.configure(text="✓ saved", fg="#4cd964")
                    self._refresh_links()
        except queue.Empty:
            pass
        if self._resave and not self._saving():
            self._resave = False
            self._mark_dirty()

    def _drop_annotated(self):
        """Everything was undone, so bin the "_2" file - a link pointing at a
        drawing that is no longer on screen is worse than no link. Only ever
        removes the file this session auto-saved."""
        if not self.anno_saved:
            return
        try:
            os.remove(self.anno_saved)
        except Exception as ex:
            _log("couldn't remove the annotated save: %s" % ex)
        self.anno_saved = None
        self.save_status.configure(text="")
        self._refresh_links()

    def _finish_pending_save(self, pending):
        """Called on the way out: a debounced edit must not die with the window.
        Waits out any write already running, then does the last one inline -
        closing can wait a beat, silently dropping the final stroke can't."""
        t = self._save_thread
        if t is not None and t.is_alive():
            t.join(timeout=10)
        self._drain_saves()      # so anno_saved reflects that last write
        if not pending or not self.saved_path:
            return
        if not self.items and not self.watermark:
            self._drop_annotated()
            return
        try:
            self.anno_saved = self._write_annotated(list(self.items),
                                                    self._wm_snapshot())
        except Exception as ex:
            _log("final annotated save failed: %s" % ex)

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
        self._commit_text()       # text still being typed is part of the drawing
        self._closed = True
        pending = self._save_after_id is not None or self._resave
        for attr in ("_tick_id", "_save_after_id"):
            tid = getattr(self, attr)
            if tid is not None:
                try:
                    self.win.after_cancel(tid)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._finish_pending_save(pending)
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
    def __init__(self, resident=False, auto_copy=True, auto_save=True,
                 save_dir=None):
        self.root = tk.Tk()
        self.root.withdraw()
        self.resident = resident and (IS_WIN or IS_MAC)
        self.auto_copy = auto_copy  # copy the plain snip as soon as it's taken
        self.auto_save = auto_save  # ...and write it to disk just as promptly
        self.save_dir = save_dir or default_save_dir()
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
        saved, save_err = self._save_static(image)
        self.state = "editing"
        self.editor = Editor(self.root, image, self.request_capture,
                             self._finish, static_copied=copied,
                             saved_path=saved, save_error=save_err)

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

    def _save_static(self, image):
        """Write the snip to disk before the editor is even up, so a capture is
        never lost to a crash or a mis-click. Best effort - a full disk mustn't
        cost the user the snip itself. Returns (path, error message)."""
        if not self.auto_save:
            return None, None
        try:
            path = save_snip(image, self.save_dir)
        except Exception as ex:
            _log(f"auto-save failed: {ex}")
            return None, str(ex)
        _log(f"snip saved to {path}")
        return path, None

    def _finish(self):
        """A snip cycle ended (cancelled or editor closed)."""
        self.editor = None
        self.state = "idle"       # resident: back to idle, tray + hotkey live
        if not self.resident:
            self.root.quit()

    def run(self):
        self.root.mainloop()


def _arg_value(flag, argv=None):
    """Read `--flag VALUE` or `--flag=VALUE` out of argv. No argparse: the CLI
    is a handful of switches and startup stays dependency-free."""
    argv = sys.argv if argv is None else argv
    for i, a in enumerate(argv):
        if a == flag and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


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
    auto_save = "--no-save" not in sys.argv
    App(resident=resident, auto_copy=auto_copy, auto_save=auto_save,
        save_dir=_arg_value("--save-dir")).run()
