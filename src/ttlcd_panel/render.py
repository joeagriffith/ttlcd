"""Rendering toolkit for 480x128 frames: cached fonts + a Canvas with the
glow / bar / text helpers shared by every view."""
import math
import os

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

W, H = 480, 128

_FONT_CANDIDATES = {
    True: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    False: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}
_font_cache = {}


def font(size, bold=True):
    key = (size, bold)
    if key not in _font_cache:
        f = None
        for path in _FONT_CANDIDATES[bool(bold)]:
            if os.path.exists(path):
                f = ImageFont.truetype(path, size)
                break
        _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]


def hsv(h, s=1.0, v=1.0):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


class Canvas:
    """A frame being drawn. Bright elements echoed to a glow layer that is
    blurred and screen-composited in finish() for a soft neon look."""

    def __init__(self, bg=(6, 8, 14)):
        self.base = Image.new("RGB", (W, H), bg)
        self.glow = Image.new("RGB", (W, H), (0, 0, 0))
        self.d = ImageDraw.Draw(self.base)
        self.g = ImageDraw.Draw(self.glow)

    # backgrounds -----------------------------------------------------------
    def gradient(self, top, bottom):
        for y in range(H):
            t = y / H
            self.d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))

    def grid(self, step=24, offset=0, color=(14, 20, 40)):
        for x in range(-step + offset % step, W, step):
            self.d.line([(x, 0), (x, H)], fill=color)
        for y in range(-step + offset % step, H, step):
            self.d.line([(0, y), (W, y)], fill=color)

    # primitives ------------------------------------------------------------
    def text(self, xy, s, f, fill, glow=False, anchor=None):
        self.d.text(xy, s, font=f, fill=fill, anchor=anchor)
        if glow:
            self.g.text(xy, s, font=f, fill=fill, anchor=anchor)

    def rtext(self, right, y, s, f, fill, glow=False):
        w = self.d.textlength(s, font=f)
        self.text((right - w, y), s, f, fill, glow=glow)

    def textlen(self, s, f):
        return self.d.textlength(s, font=f)

    def rect(self, box, fill=None, outline=None, width=1, glow=False):
        self.d.rectangle(box, fill=fill, outline=outline, width=width)
        if glow and fill:
            self.g.rectangle(box, fill=fill)

    def bar(self, box, frac, fill, outline=(40, 55, 80), glow=True):
        x0, y0, x1, y1 = box
        self.d.rectangle(box, outline=outline)
        fx = x0 + int((x1 - x0) * max(0.0, min(1.0, frac)))
        if fx > x0:
            self.d.rectangle([x0, y0, fx, y1], fill=fill)
            if glow:
                self.g.rectangle([x0, y0, fx, y1], fill=fill)

    def line(self, pts, fill, width=1, glow=False):
        self.d.line(pts, fill=fill, width=width)
        if glow:
            self.g.line(pts, fill=fill, width=width)

    def ellipse(self, box, fill=None, outline=None, width=1, glow=False):
        self.d.ellipse(box, fill=fill, outline=outline, width=width)
        if glow and fill:
            self.g.ellipse(box, fill=fill)

    def arc(self, box, start, end, fill, width=1):
        self.d.arc(box, start, end, fill=fill, width=width)

    def sparkle(self, cx, cy, r, color, glow=True):
        self.line([(cx - r, cy), (cx + r, cy)], color, 2, glow=glow)
        self.line([(cx, cy - r), (cx, cy + r)], color, 2, glow=glow)
        self.line([(cx - r * 0.6, cy - r * 0.6), (cx + r * 0.6, cy + r * 0.6)], color, 1)
        self.line([(cx - r * 0.6, cy + r * 0.6), (cx + r * 0.6, cy - r * 0.6)], color, 1)

    # output ----------------------------------------------------------------
    def finish(self, blur=2):
        glow = self.glow.filter(ImageFilter.GaussianBlur(blur))
        return ImageChops.screen(self.base, glow)
