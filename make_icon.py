"""Generate icon.ico for SnipSquiggle: a snip frame with a squiggly stroke."""
import math
from PIL import Image, ImageDraw

SS = 8  # supersample factor for smooth edges


def rounded_rect(d, box, r, **kw):
    d.rounded_rectangle(box, radius=r, **kw)


def draw_icon(size):
    s = size * SS
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    m = s * 0.06
    # background: dark rounded square with a subtle blue edge
    rounded_rect(d, (m, m, s - m, s - m), r=s * 0.22,
                 fill=(30, 30, 34, 255))
    rounded_rect(d, (m, m, s - m, s - m), r=s * 0.22,
                 outline=(10, 132, 255, 255), width=max(1, int(s * 0.012)))

    # crop-corner brackets (the "snip" motif)
    cm = s * 0.20          # corner inset
    ln = s * 0.16          # bracket arm length
    cw = max(2, int(s * 0.028))
    col = (255, 255, 255, 235)
    for cx, cy, dx, dy in ((cm, cm, 1, 1), (s - cm, cm, -1, 1),
                           (cm, s - cm, 1, -1), (s - cm, s - cm, -1, -1)):
        d.line((cx, cy, cx + dx * ln, cy), fill=col, width=cw)
        d.line((cx, cy, cx, cy + dy * ln), fill=col, width=cw)

    # the squiggly stroke through the middle
    pts = []
    x0, x1 = s * 0.24, s * 0.76
    n = 60
    for i in range(n + 1):
        t = i / n
        x = x0 + (x1 - x0) * t
        y = s * 0.52 + math.sin(t * math.pi * 3.2) * s * 0.11
        pts.append((x, y))
    d.line(pts, fill=(255, 59, 48, 255), width=max(3, int(s * 0.055)),
           joint="curve")
    r = s * 0.028
    for (x, y) in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 59, 48, 255))

    return im.resize((size, size), Image.LANCZOS)


sizes = [16, 24, 32, 48, 64, 128, 256]
imgs = [draw_icon(sz) for sz in sizes]
imgs[0].save("icon.ico", format="ICO",
             sizes=[(sz, sz) for sz in sizes], append_images=imgs[1:])
# also a png preview
draw_icon(256).save("icon.png")
print("wrote icon.ico and icon.png")
