"""View classes for the 480x128 panel. Each ``View.render(ctx) -> PIL.Image``
builds a :class:`render.Canvas`, draws from the read-only ``ctx``, and returns
``canvas.finish()``. Views keep their own animation state on ``self`` and never
touch hardware, USB, or the network — they only read the cached ``ctx`` dict.

``ctx`` attributes used (see ARCHITECTURE.md "View context"):
    ctx.frame    int    increments every rendered frame
    ctx.t        float   wall-clock seconds (animation / elapsed)
    ctx.metrics  dict    Collector.snapshot() (cpu/ram/gpu/...)
    ctx.run      dict|None  run-state
    ctx.message  dict|None  {"text", "level", "until"}
"""
from __future__ import annotations

import math
import random
import time

from .render import Canvas, font, hsv, W, H


# --- shared palette ---------------------------------------------------------
ACCENT = (0, 220, 200)
GOOD = (90, 235, 130)
DIM = (140, 158, 175)
WHITE = (235, 240, 245)


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _fmt_num(v):
    """Format a scalar metric compactly: big ints plain, small floats with a
    sensible number of significant digits, very small/large in scientific."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if v != v:  # NaN
        return "nan"
    av = abs(v)
    if av == 0:
        return "0"
    if av >= 1e4 or av < 1e-3:
        return "%.1e" % v
    if av >= 100:
        return "%.0f" % v
    if av >= 10:
        return "%.2f" % v
    return "%.3f" % v


class View:
    """Base view. Subclasses implement ``render(ctx) -> PIL.Image``."""

    name = "view"

    def render(self, ctx):  # pragma: no cover - abstract
        raise NotImplementedError

    # convenience for subclasses
    @staticmethod
    def _canvas(bg=(6, 8, 14)):
        return Canvas(bg=bg)


# ---------------------------------------------------------------------------
class SystemView(View):
    """Idle default: neon clock, CPU% + per-core mini-bars, GPU panel, RAM bar.
    Cyberpunk aesthetic ported from layouts.Cool."""

    name = "system"

    def render(self, ctx):
        m = ctx.metrics or {}
        f = ctx.frame
        cpu = m.get("cpu") or {}
        ram = m.get("ram") or {}
        gpu = m.get("gpu")  # may be None

        cpu_pct = float(cpu.get("pct", 0.0) or 0.0)
        per_core = cpu.get("per_core") or []
        ram_pct = float(ram.get("pct", 0.0) or 0.0)

        accent_hue = (0.5 + 0.08 * math.sin(f / 18.0)) % 1.0
        accent = hsv(accent_hue, 1.0, 1.0)
        accent_dim = hsv(accent_hue, 0.45, 1.0)

        c = self._canvas(bg=(4, 5, 12))
        c.gradient((6, 7, 18), (16, 15, 44))
        c.grid(step=24, offset=f % 24, color=(14, 20, 40))

        f_big = font(40, bold=True)
        f_mid = font(15, bold=True)
        f_sm = font(11, bold=False)
        f_lbl = font(10, bold=True)

        # --- big glowing clock ---
        clock = time.strftime("%H:%M:%S")
        date = time.strftime("%a %d %b")
        pulse = 0.78 + 0.22 * abs(math.sin(f / 6.0))
        c.text((14, 6), clock, f_big, tuple(int(v * pulse) for v in accent), glow=True)
        c.text((16, 50), date, f_sm, (170, 190, 220))

        # --- CPU label + value ---
        c.text((14, 70), "CPU", f_lbl, accent_dim)
        c.text((54, 66), "%3.0f%%" % cpu_pct, f_mid, accent)

        # --- per-core mini-bars (under the clock, left cluster) ---
        n = min(len(per_core), 16) if per_core else 0
        bw, gap = 6, 3
        bx0 = 14
        base_y = 100
        for i in range(n):
            cv = _clamp(float(per_core[i] or 0.0) / 100.0)
            bx = bx0 + i * (bw + gap)
            bh = int(2 + cv * 22)
            hue = (accent_hue + 0.12 * (i / max(1, n))) % 1.0
            col = hsv(hue, 1.0, 1.0)
            c.rect([bx, base_y - bh, bx + bw, base_y], fill=col, glow=True)

        # --- GPU panel (right side) ---
        gx = 270
        c.line([(gx - 10, 8), (gx - 10, H - 8)], (24, 34, 56), 1)
        if gpu is None:
            c.text((gx, 12), "GPU", f_lbl, accent_dim)
            c.text((gx, 30), "NO GPU", f_mid, (120, 130, 150))
        else:
            name = str(gpu.get("name", "GPU"))
            util = float(gpu.get("util", 0.0) or 0.0)
            temp = float(gpu.get("temp_c", 0.0) or 0.0)
            mem_used = float(gpu.get("mem_used_gb", 0.0) or 0.0)
            mem_total = float(gpu.get("mem_total_gb", 0.0) or 0.0)
            mem_frac = _clamp(mem_used / mem_total) if mem_total else 0.0

            # truncate long names to fit
            disp = name
            while c.textlen(disp, f_sm) > (W - gx - 8) and len(disp) > 4:
                disp = disp[:-1]
            c.text((gx, 8), disp, f_sm, (170, 190, 220))

            c.text((gx, 26), "UTIL", f_lbl, accent_dim)
            c.text((gx + 44, 22), "%3.0f%%" % util, f_mid, GOOD)
            c.text((gx + 110, 26), "TEMP", f_lbl, accent_dim)
            tcol = hsv((0.33 - 0.33 * _clamp(temp / 95.0)) % 1.0, 1.0, 1.0)
            c.text((gx + 152, 22), "%2.0fC" % temp, f_mid, tcol)

            c.text((gx, 48), "VRAM", f_lbl, accent_dim)
            c.bar([gx + 44, 49, W - 12, 59], mem_frac, GOOD)
            c.text((gx, 64), "%.1f / %.1f GB" % (mem_used, mem_total),
                   f_sm, (170, 190, 220))

        # --- RAM bar (bottom right) ---
        rb_x0, rb_x1 = W - 168, W - 12
        rb_y = H - 14
        c.text((rb_x0, rb_y - 16), "RAM", f_sm, accent_dim)
        mem_col = hsv((0.33 - 0.33 * ram_pct / 100.0) % 1.0, 1.0, 1.0)
        c.bar([rb_x0 + 34, rb_y, rb_x1, rb_y + 8], ram_pct / 100.0, mem_col)
        c.text((rb_x1 - 34, rb_y - 16), "%2.0f%%" % ram_pct, f_sm, mem_col)

        return c.finish(blur=3)


# ---------------------------------------------------------------------------
class TrainingView(View):
    """ML dashboard: headline metrics, epoch progress bar, 128-cell SM heatmap
    driven by real GPU util. Ported from layouts.Train."""

    name = "training"

    SM = 128
    SM_COLS = 16
    SM_ROWS = 8

    def __init__(self):
        # smoothed per-cell SM utilization (0..1)
        self.sm = [random.uniform(0.3, 0.6) for _ in range(self.SM)]

    @staticmethod
    def _heat(u):
        """Map 0..1 to dark -> green -> bright-green."""
        u = _clamp(u)
        if u < 0.12:
            return (16, 26, 22)
        r = int(20 + 70 * u)
        g = int(70 + 175 * u)
        b = int(24 + 40 * u)
        return (r, g, b)

    def _update_sm(self, target):
        target = _clamp(target)
        for i in range(self.SM):
            tc = _clamp(target + random.uniform(-0.28, 0.28))
            self.sm[i] += 0.35 * (tc - self.sm[i])

    def render(self, ctx):
        f = ctx.frame
        run = ctx.run
        m = ctx.metrics or {}
        gpu = m.get("gpu")

        c = self._canvas(bg=(6, 8, 14))
        c.gradient((6, 8, 14), (12, 15, 28))

        f_big = font(33, bold=True)
        f_unit = font(13, bold=True)
        f_lbl = font(10, bold=True)
        f_sm = font(11, bold=False)

        # --- SM utilization target from real GPU util (or idle shimmer) ---
        if gpu is not None:
            util = _clamp(float(gpu.get("util", 0.0) or 0.0) / 100.0)
            target = util + 0.04 * math.sin(f / 15.0)
        else:
            target = 0.22 + 0.14 * (0.5 + 0.5 * math.sin(f / 22.0))
        self._update_sm(target)
        sm_util = sum(self.sm) / self.SM

        if run is None:
            # faint waiting state
            c.grid(step=24, offset=f % 24, color=(12, 16, 30))
            msg = "waiting for run…"
            fw = font(20, bold=True)
            tw = c.textlen(msg, fw)
            glow = 0.35 + 0.25 * (0.5 + 0.5 * math.sin(f / 10.0))
            c.text(((W - tw) / 2, H / 2 - 14), msg, fw,
                   tuple(int(v * glow) for v in ACCENT))
            return c.finish(blur=2)

        # --- run header ---
        project = str(run.get("project", "run"))
        run_id = str(run.get("run_id", ""))
        status = str(run.get("status", "running"))
        owner = str(run.get("owner", "") or "").strip()

        # rotation badge (top-right corner) when the dashboard cycles between
        # several concurrent runs. Hidden for a single run / when absent.
        rot = run.get("_rotation")
        try:
            rot_cur, rot_tot = int(rot[0]), int(rot[1])
        except (TypeError, ValueError, IndexError):
            rot_cur = rot_tot = 0
        right_edge = W - 8
        if rot_tot > 1:
            badge = "▸ %d/%d" % (rot_cur, rot_tot)
            bw = c.textlen(badge, f_lbl)
            bx = (W - 5) - bw
            c.rect([bx - 4, 0, W - 1, 13], fill=(16, 30, 38))
            c.text((bx, 2), badge, f_lbl, ACCENT)
            right_edge = bx - 8

        st_col = {"running": ACCENT, "finished": GOOD, "failed": (235, 90, 90)}.get(
            status, DIM)
        st_up = status.upper()
        c.rtext(right_edge, 1, st_up, f_sm, st_col)

        # left side: compact owner tag (accent) + project + run id (dim),
        # truncated to whatever room is left before the status text.
        avail = right_edge - int(c.textlen(st_up, f_sm)) - 12 - 8
        hx = 8
        if owner:
            otag = owner[:18]
            c.text((hx, 1), otag, f_sm, ACCENT)
            hx += int(c.textlen(otag, f_sm))
            sep = " · "
            c.text((hx, 1), sep, f_sm, DIM)
            hx += int(c.textlen(sep, f_sm))
        tail = "%s  %s" % (project, run_id)
        while tail and (hx + c.textlen(tail, f_sm)) > (8 + max(40, avail)):
            tail = tail[:-1]
        c.text((hx, 1), tail, f_sm, DIM)

        # --- headline metrics: loss + acc always, then a few extras ---
        metrics = run.get("metrics") or {}
        c.text((8, 16), "LOSS", f_lbl, ACCENT)
        loss = metrics.get("loss")
        c.text((6, 26), _fmt_num(loss) if loss is not None else "--",
               f_big, WHITE, glow=True)

        c.text((150, 16), "ACC", f_lbl, ACCENT)
        acc = metrics.get("acc", metrics.get("accuracy"))
        acc_s = _fmt_num(acc) if acc is not None else "--"
        c.text((148, 26), acc_s, f_big, GOOD, glow=True)

        # secondary metrics row: up to 3 other scalars
        skip = {"loss", "acc", "accuracy"}
        extras = [(k, v) for k, v in metrics.items()
                  if k not in skip and isinstance(v, (int, float))][:3]
        ex_x = 8
        for k, v in extras:
            label = k.upper()[:6]
            c.text((ex_x, 64), label, f_lbl, ACCENT)
            lw = c.textlen(label, f_lbl)
            c.text((ex_x + lw + 4, 63), _fmt_num(v), f_unit, WHITE)
            ex_x += int(lw + 4 + c.textlen(_fmt_num(v), f_unit) + 14)

        # --- SM utilization heatmap (right column) ---
        gx0, gy0 = 312, 26
        gw = (W - 6) - gx0
        cw = gw / self.SM_COLS
        ch = 7.6
        c.text((gx0, 14), "SM UTIL", f_lbl, ACCENT)
        c.rtext(W - 6, 14, "%2.0f%%" % (sm_util * 100), f_lbl, GOOD)
        for r in range(self.SM_ROWS):
            for col_i in range(self.SM_COLS):
                u = self.sm[r * self.SM_COLS + col_i]
                x = gx0 + col_i * cw
                y = gy0 + r * ch
                hcol = self._heat(u)
                glow = u > 0.82
                c.rect([x, y, x + cw - 1.6, y + ch - 1.6], fill=hcol, glow=glow)

        # --- epoch progress bar ---
        epochs = run.get("epochs")
        spe = run.get("steps_per_epoch")
        epoch = int(run.get("epoch", 0) or 0)
        batch = int(run.get("batch", 0) or 0)

        ep_label = "EPOCH %d/%s" % (epoch + 1, epochs if epochs else "?")
        c.text((8, 97), ep_label, f_lbl, ACCENT)

        by0, by1 = 110, 122
        bx0, bx1 = 8, W - 8
        if spe:
            frac = _clamp((batch) / spe)
            c.rtext(W - 8, 97, "batch %d/%d" % (batch, spe), f_lbl, DIM)
            c.bar([bx0, by0, bx1, by1], frac, ACCENT, outline=(38, 52, 72))
        else:
            # indeterminate: a sliding pip
            c.rect([bx0, by0, bx1, by1], outline=(38, 52, 72))
            seg = 90
            pos = (f * 6) % (bx1 - bx0 + seg) - seg
            sx0 = bx0 + max(0, pos)
            sx1 = bx0 + min(bx1 - bx0, pos + seg)
            if sx1 > sx0:
                c.rect([sx0, by0, sx1, by1], fill=ACCENT, glow=True)
            c.rtext(W - 8, 97, "batch %d" % batch, f_lbl, DIM)

        return c.finish(blur=2)


# ---------------------------------------------------------------------------
class MessageView(View):
    """Full-screen centered card showing ctx.message["text"], colored by level."""

    name = "message"

    LEVELS = {
        "info": (0, 200, 220),
        "warn": (240, 180, 40),
        "error": (235, 70, 70),
    }

    def _wrap(self, c, text, fnt, max_w):
        """Greedy word-wrap to fit max_w pixels per line."""
        lines = []
        for para in text.split("\n"):
            words = para.split(" ")
            cur = ""
            for w in words:
                trial = w if not cur else cur + " " + w
                if c.textlen(trial, fnt) <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = w
            lines.append(cur)
        return lines

    def render(self, ctx):
        msg = ctx.message or {}
        text = str(msg.get("text", ""))
        level = msg.get("level", "info")
        color = self.LEVELS.get(level, self.LEVELS["info"])
        f = ctx.frame

        c = self._canvas(bg=(8, 10, 16))
        c.gradient((8, 10, 16), (14, 12, 22))

        # card
        pad = 10
        cx0, cy0, cx1, cy1 = pad, pad, W - pad, H - pad
        c.rect([cx0, cy0, cx1, cy1], fill=(14, 18, 28))
        pulse = 0.7 + 0.3 * abs(math.sin(f / 8.0))
        border = tuple(int(v * pulse) for v in color)
        c.rect([cx0, cy0, cx1, cy1], outline=border, width=2, glow=True)
        # accent strip on the left
        c.rect([cx0, cy0, cx0 + 5, cy1], fill=color, glow=True)

        max_w = (cx1 - cx0) - 28
        max_h = (cy1 - cy0) - 16

        # pick the largest font size that fits both width and height
        chosen = None
        for size in (34, 28, 24, 20, 16, 13, 11):
            fnt = font(size, bold=True)
            lines = self._wrap(c, text, fnt, max_w)
            line_h = size + 4
            if len(lines) * line_h <= max_h:
                chosen = (fnt, lines, line_h)
                break
        if chosen is None:
            fnt = font(11, bold=True)
            lines = self._wrap(c, text, fnt, max_w)
            line_h = 14
            # clip to fit
            max_lines = max(1, max_h // line_h)
            lines = lines[:max_lines]
            chosen = (fnt, lines, line_h)
        fnt, lines, line_h = chosen

        total_h = len(lines) * line_h
        y = cy0 + ((cy1 - cy0) - total_h) // 2
        for ln in lines:
            lw = c.textlen(ln, fnt)
            x = cx0 + 18 + ((max_w - lw) // 2)
            c.text((x, y), ln, fnt, WHITE, glow=True)
            y += line_h

        return c.finish(blur=2)


# ---------------------------------------------------------------------------
class MascotView(View):
    """Claude run-in / knock / wave / run-off loop, ported from claude_anim.py
    onto the Canvas API. Driven by ctx.frame."""

    name = "mascot"

    GROUND = 104
    CENTER_X = 232

    CORAL = (222, 122, 90)
    CORAL_D = (190, 96, 68)
    CORAL_L = (240, 156, 124)
    CREAM = (247, 240, 228)
    BLUSH = (255, 158, 138)
    EYE = (44, 32, 40)
    BUBBLE = (250, 246, 238)

    RUN_IN_END = 42
    KNOCK_END = 84
    WAVE_END = 128
    RUNOFF_END = 168
    CYCLE = 182

    @staticmethod
    def _ease(t):
        t = _clamp(t)
        return t * t * (3 - 2 * t)

    @staticmethod
    def _limb(c, p0, p1, w, col, hand_r=0, hand_col=None):
        c.line([p0, p1], col, w, glow=True)
        if hand_r:
            hc = hand_col or col
            box = [p1[0] - hand_r, p1[1] - hand_r, p1[0] + hand_r, p1[1] + hand_r]
            c.ellipse(box, fill=hc, glow=True)

    def _body(self, c, cx, by, facing, blink, look_dx=0):
        bw, bh = 40, 46
        x0, y0 = cx - bw // 2, by - bh // 2
        x1, y1 = cx + bw // 2, by + bh // 2
        c.ellipse([x0, y0, x1, y1], fill=self.CORAL, outline=self.CORAL_D, width=2)
        c.ellipse([x0 + 4, y0 - 6, x0 + 14, y0 + 6], fill=self.CORAL)
        c.ellipse([x1 - 14, y0 - 6, x1 - 4, y0 + 6], fill=self.CORAL)
        if facing == "front":
            c.ellipse([cx - 12, by - 2, cx + 12, y1 - 3], fill=self.CORAL_L)
            ey = by - 8
            for sx in (-9, 9):
                ex = cx + sx
                c.ellipse([ex - 6, ey - 7, ex + 6, ey + 7], fill=self.CREAM)
                if blink:
                    c.line([(ex - 5, ey), (ex + 5, ey)], self.EYE, 2)
                else:
                    c.ellipse([ex - 3 + look_dx, ey - 2, ex + 3 + look_dx, ey + 4],
                              fill=self.EYE)
                    c.ellipse([ex - 2 + look_dx, ey - 1, ex + look_dx, ey + 1],
                              fill=self.CREAM)
            c.ellipse([cx - 18, by + 1, cx - 11, by + 6], fill=self.BLUSH)
            c.ellipse([cx + 11, by + 1, cx + 18, by + 6], fill=self.BLUSH)
            c.arc([cx - 7, by + 1, cx + 7, by + 12], 15, 165, fill=self.EYE, width=2)
        else:
            c.ellipse([cx - 12, by - 4, cx + 12, y1 - 5], fill=self.CORAL_D)

    def _run_pose(self, c, cx, u, facing, blink):
        ph = u * 1.1
        bob = abs(math.sin(ph)) * 3
        by = self.GROUND - 30 - bob
        for sgn, off in ((-1, 0.0), (1, math.pi)):
            fx = cx + sgn * 7 + 6 * math.cos(ph + off)
            fy = self.GROUND - max(0.0, math.sin(ph + off)) * 6
            self._limb(c, (cx + sgn * 6, by + 18), (fx, fy + 6), 6,
                       self.CORAL_D, hand_r=4, hand_col=self.CORAL_D)
        for sgn, off in ((-1, math.pi), (1, 0.0)):
            hx = cx + sgn * 20 + 5 * math.cos(ph + off)
            hy = by + 4 + 4 * math.sin(ph + off)
            self._limb(c, (cx + sgn * 16, by - 2), (hx, hy), 6,
                       self.CORAL, hand_r=5, hand_col=self.CORAL_L)
        self._body(c, cx, by, facing, blink)
        c.sparkle(cx + 22, by - 30 - bob, 4, self.CREAM)

    def _stand(self, c, cx, blink, look_dx=0):
        by = self.GROUND - 30
        for sgn in (-1, 1):
            self._limb(c, (cx + sgn * 6, by + 18), (cx + sgn * 9, self.GROUND + 6),
                       6, self.CORAL_D, hand_r=4, hand_col=self.CORAL_D)
        self._limb(c, (cx - 16, by - 2), (cx - 22, by + 12), 6,
                   self.CORAL, hand_r=5, hand_col=self.CORAL_L)
        self._body(c, cx, by, "front", blink, look_dx=look_dx)
        c.sparkle(cx - 24, by - 26, 4, self.CREAM)

    def _bubble(self, c, x, y, text):
        f = font(17, bold=True)
        tw = c.textlen(text, f)
        pad = 7
        x0, y0 = x, y
        x1, y1 = x + tw + pad * 2, y + 22
        c.d.rounded_rectangle([x0, y0, x1, y1], radius=7, fill=self.BUBBLE)
        c.g.rounded_rectangle([x0, y0, x1, y1], radius=7, fill=(120, 116, 110))
        c.d.polygon([(x0 + 6, y1 - 2), (x0 - 6, y1 + 10), (x0 + 14, y1 - 2)],
                    fill=self.BUBBLE)
        c.text((x0 + pad, y0 + 3), text, f, (214, 96, 70))
        hx, hy = x1 - 4, y0 - 2
        c.ellipse([hx - 4, hy - 3, hx, hy + 1], fill=self.BLUSH)
        c.ellipse([hx, hy - 3, hx + 4, hy + 1], fill=self.BLUSH)
        c.d.polygon([(hx - 4, hy), (hx + 4, hy), (hx, hy + 5)], fill=self.BLUSH)

    def render(self, ctx):
        tt = ctx.frame
        u = tt % self.CYCLE

        c = self._canvas(bg=(26, 22, 34))
        c.gradient((26, 22, 34), (40, 32, 46))

        # twinkly stars
        stars = [(40, 26), (120, 16), (210, 30), (330, 18),
                 (300, 40), (430, 24), (90, 44), (390, 46)]
        for i, (sx, sy) in enumerate(stars):
            tw = 0.5 + 0.5 * math.sin(tt / 7.0 + i)
            col = int(90 + 120 * tw)
            c.d.point([(sx, sy)], fill=(col, col - 20, col - 40))
            if tw > 0.85:
                c.sparkle(sx, sy, 2, (200, 190, 150))

        c.line([(0, self.GROUND + 8), (W, self.GROUND + 8)], (54, 44, 60), 2)

        blink = (tt % 46) < 2

        if u < self.RUN_IN_END:                       # run in from left
            p = self._ease(u / self.RUN_IN_END)
            cx = int(-50 + (self.CENTER_X + 50) * p)
            self._run_pose(c, cx, u, facing="front", blink=blink)

        elif u < self.KNOCK_END:                      # knock on glass
            cx = self.CENTER_X
            k = u - self.RUN_IN_END
            self._stand(c, cx, blink, look_dx=1)
            beat = math.sin(k * 0.9)
            fist_y = self.GROUND - 44 - max(0, beat) * 6
            fist_x = cx + 20 + max(0, beat) * 3
            root = (cx + 14, self.GROUND - 40)
            self._limb(c, root, (fist_x, fist_y), 7, self.CORAL,
                       hand_r=6, hand_col=self.CORAL_L)
            if beat > 0.6:
                c.arc([fist_x + 4, fist_y - 8, fist_x + 16, fist_y + 8],
                      -60, 60, fill=self.CREAM, width=2)
                c.arc([fist_x + 8, fist_y - 12, fist_x + 24, fist_y + 12],
                      -50, 50, fill=(150, 140, 120), width=1)
            if (k % 28) < 16:
                c.text((cx - 24, self.GROUND - 70), "knock!",
                       font(11, bold=True), self.CREAM)

        elif u < self.WAVE_END:                       # wave hello
            cx = self.CENTER_X
            w = u - self.KNOCK_END
            self._stand(c, cx, blink, look_dx=0)
            ang = math.radians(-118 + 26 * math.sin(w * 0.7))
            root = (cx + 14, self.GROUND - 42)
            L = 22
            hand = (root[0] + L * math.cos(ang), root[1] + L * math.sin(ang))
            self._limb(c, root, hand, 7, self.CORAL, hand_r=6, hand_col=self.CORAL_L)
            c.arc([hand[0] - 14, hand[1] - 12, hand[0] + 2, hand[1] + 4],
                  200, 320, fill=(150, 140, 120), width=1)
            self._bubble(c, cx + 34, self.GROUND - 78, "hi!")

        elif u < self.RUNOFF_END:                     # turn & run off right
            p = self._ease((u - self.WAVE_END) / (self.RUNOFF_END - self.WAVE_END))
            cx = int(self.CENTER_X + (W + 60 - self.CENTER_X) * p)
            self._run_pose(c, cx, u, facing="back", blink=False)
            if p < 0.5:
                c.text((cx - 46, self.GROUND - 70), "bye! <3",
                       font(11, bold=True), self.CREAM)
        # else: brief empty beat before looping

        return c.finish(blur=2)


# ---------------------------------------------------------------------------
class OutcomeView(View):
    """Final-outcome screen for a finished/crashed run. Two modes keyed on
    ``ctx.run["status"]``: a green celebratory COMPLETE card (with a few
    classy animated sparkles) and a red alarm CRASHED card."""

    name = "outcome"

    BAD = (235, 80, 80)

    @staticmethod
    def _elapsed(run):
        try:
            dt = float(run.get("updated_at", 0)) - float(run.get("started_at", 0))
        except (TypeError, ValueError):
            return None
        if dt < 0 or dt != dt:
            return None
        m, s = divmod(int(dt), 60)
        return "%d:%02d" % (m, s)

    @staticmethod
    def _trunc(c, s, fnt, max_w):
        s = str(s)
        if c.textlen(s, fnt) <= max_w:
            return s
        while s and c.textlen(s + "…", fnt) > max_w:
            s = s[:-1]
        return s + "…"

    def _owner_project(self, c, run, fnt, max_w):
        owner = str(run.get("owner", "") or "").strip()
        project = str(run.get("project", "run") or "run").strip()
        if owner:
            return self._trunc(c, "%s · %s" % (owner, project), fnt, max_w)
        return self._trunc(c, project, fnt, max_w)

    def _metric_pairs(self, run):
        """Ordered (LABEL, value-str) for loss, acc, then up to 2 extras."""
        metrics = run.get("metrics") or {}
        pairs = []
        loss = metrics.get("loss")
        if loss is not None:
            pairs.append(("LOSS", _fmt_num(loss)))
        acc = metrics.get("acc", metrics.get("accuracy"))
        if acc is not None:
            pairs.append(("ACC", _fmt_num(acc)))
        skip = {"loss", "acc", "accuracy"}
        for k, v in metrics.items():
            if len(pairs) >= 4:
                break
            if k in skip or not isinstance(v, (int, float)):
                continue
            pairs.append((k.upper()[:6], _fmt_num(v)))
        return pairs

    def render(self, ctx):
        run = ctx.run or {}
        f = ctx.frame
        failed = str(run.get("status", "")) == "failed"
        accent = self.BAD if failed else GOOD

        c = self._canvas(bg=(6, 8, 14))
        if failed:
            c.gradient((18, 7, 9), (10, 6, 10))
        else:
            c.gradient((6, 14, 12), (10, 18, 22))

        f_head = font(34, bold=True)
        f_big = font(30, bold=True)
        f_lbl = font(10, bold=True)
        f_sm = font(11, bold=False)
        f_unit = font(13, bold=True)

        # accent strip on the left, gently pulsing
        pulse = 0.6 + 0.4 * abs(math.sin(f / 9.0))
        c.rect([0, 0, 5, H], fill=tuple(int(v * pulse) for v in accent), glow=True)

        epochs = run.get("epochs")
        epoch = int(run.get("epoch", 0) or 0)
        elapsed = self._elapsed(run)
        pairs = self._metric_pairs(run)

        # rotation badge (top-right) when cycling several finished runs
        rot = run.get("_rotation")
        right_edge = W - 10
        try:
            rc, rt = int(rot[0]), int(rot[1])
        except (TypeError, ValueError, IndexError):
            rc = rt = 0
        if rt > 1:
            badge = "▸ %d/%d" % (rc, rt)
            bw = c.textlen(badge, f_lbl)
            c.text((right_edge - bw, 3), badge, f_lbl, DIM)

        # --- headline -------------------------------------------------------
        if failed:
            head = "✕ CRASHED"
        else:
            head = "✓ COMPLETE"
        glow_col = tuple(int(v * (0.7 + 0.3 * pulse)) for v in accent) \
            if failed else WHITE
        c.text((14, 6), head, f_head, glow_col, glow=True)

        # owner · project (under headline)
        op = self._owner_project(c, run, f_sm, W - 26)
        c.text((14, 46), op, f_sm, DIM)

        if failed:
            # where it died
            where = "epoch %d/%s" % (epoch + 1, epochs if epochs else "?")
            c.text((14, 64), "DIED AT", f_lbl, self.BAD)
            wl = c.textlen("DIED AT", f_lbl)
            c.text((18 + wl, 62), where, f_unit, WHITE)
        else:
            # epoch / elapsed summary line
            done = "%d epochs" % (epochs if epochs else epoch + 1)
            summ = done
            if elapsed:
                summ += "   ·   " + elapsed
            c.text((14, 64), "FINISHED", f_lbl, GOOD)
            fl = c.textlen("FINISHED", f_lbl)
            c.text((18 + fl, 62), summ, f_unit, WHITE)

        # --- final metrics row ---------------------------------------------
        my = 84
        mx = 14
        if pairs:
            for label, val in pairs:
                c.text((mx, my + 2), label, f_lbl, accent)
                lw = c.textlen(label, f_lbl)
                vx = mx + lw + 5
                c.text((vx, my), val, f_unit, WHITE, glow=not failed)
                vw = c.textlen(val, f_unit)
                mx = vx + vw + 18
                if mx > W - 60:
                    break
        else:
            c.text((mx, my), "no metrics", f_sm, DIM)

        # --- footer ---------------------------------------------------------
        if failed:
            hint = "check logs ›"
            hw = c.textlen(hint, f_sm)
            blink = 0.5 + 0.5 * math.sin(f / 6.0)
            hc = tuple(int(v * (0.55 + 0.45 * blink)) for v in self.BAD)
            c.text((14, H - 16), hint, f_sm, hc, glow=True)
        else:
            foot = []
            foot.append("%d epochs" % (epochs if epochs else epoch + 1))
            if elapsed:
                foot.append(elapsed + " elapsed")
            c.text((14, H - 16), "   ·   ".join(foot), f_sm, DIM)

        # --- celebration sparkles (finished only) --------------------------
        if not failed:
            spots = [(360, 22, 4), (412, 50, 3), (446, 18, 5),
                     (388, 96, 4), (336, 70, 3), (462, 84, 4)]
            for i, (sx, sy, sr) in enumerate(spots):
                tw = 0.5 + 0.5 * math.sin(f / 5.0 + i * 1.7)
                if tw > 0.55:
                    col = tuple(int(v * (0.5 + 0.5 * tw)) for v in GOOD)
                    c.sparkle(sx, sy, sr * (0.6 + 0.4 * tw), col)

        return c.finish(blur=2)
