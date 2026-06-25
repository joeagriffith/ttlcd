# ARCHITECTURE & INTERFACE CONTRACTS

This is the source of truth for module boundaries. Engineers: build exactly to
these signatures so everything integrates. Panel is **480x128** RGB.

## Module map (`src/ttlcd_panel/`)
- `driver.py`    ✅ DONE — `LcdDriver(render_fn, logger, image_path, orientation)`; `.setup()/.start()/.stop()/.reset_usb()`. Calls `render_fn() -> PIL.Image (480x128)` per frame (a few fps).
- `render.py`    ✅ DONE — `font(size,bold)`, `hsv(h,s,v)`, and `Canvas` (see below).
- `metrics.py`   ENGINEER A — system + GPU `Collector`.
- `views.py`     ENGINEER C — `View` subclasses, each `.render(ctx) -> PIL.Image`.
- `manager.py`   LEAD — `ViewManager`: holds run/message state, picks active view, exposes `render()` (the driver's callback) + state mutators.
- `server.py`    LEAD — FastAPI app over the manager + collector.
- `daemon.py`    LEAD — wires collector + driver + manager + server; `main()`.
- `client.py`    ENGINEER B — `Panel` SDK (HTTP client).
- `cli.py`       ENGINEER B — `panel` command (send message, status, etc.).

## render.Canvas (already built — use it)
```python
c = Canvas(bg=(6,8,14))
c.gradient(top_rgb, bottom_rgb); c.grid(step, offset, color)
c.text((x,y), s, font, fill, glow=False); c.rtext(right, y, s, font, fill)
c.textlen(s, font); c.rect(box, fill, outline, width, glow); c.bar(box, frac, fill)
c.line(pts, fill, width, glow); c.ellipse(box, ...); c.arc(box, a, b, fill, width)
c.sparkle(cx, cy, r, color)
img = c.finish(blur=2)   # -> 480x128 PIL.Image (composites glow)
from .render import font, hsv  # font cache + hsv->rgb
```

## metrics.Collector  (ENGINEER A)
```python
class Collector:
    def __init__(self, interval: float = 1.0, logger=None): ...
    def start(self): ...            # spawn daemon thread, non-blocking
    def stop(self): ...
    def snapshot(self) -> dict: ...  # latest cached values; NEVER blocks/raises
```
`snapshot()` returns this exact schema (use `None` for gpu if absent; 0.0 fallbacks, never raise):
```python
{
  "ts": float,
  "cpu": {"pct": float, "per_core": [float, ...], "freq_mhz": float, "load1": float},
  "ram": {"pct": float, "used_gb": float, "total_gb": float},
  "net": {"up_mbps": float, "down_mbps": float},
  "gpu": {  # or None
    "present": True, "name": str, "util": float,        # 0..100
    "mem_used_gb": float, "mem_total_gb": float, "mem_pct": float,
    "temp_c": float, "power_w": float, "fan_pct": float,
  } | None,
}
```
GPU: prefer `pynvml` (pip `nvidia-ml-py`); fall back to parsing `nvidia-smi --query-gpu=...`; if neither, `gpu=None`. Poll GPU at the same interval. Keep CPU `per_core` length stable.

## View context (`ctx`) passed to `View.render(ctx)`  (ENGINEER C)
A simple object/dataclass with attributes:
```python
ctx.frame    # int, increments every rendered frame
ctx.t        # float wall-clock seconds (use for animation/elapsed)
ctx.metrics  # dict from Collector.snapshot()
ctx.run      # dict run-state or None (schema below)
ctx.message  # dict or None (schema below)
```
Run-state dict:
```python
{
  "run_id": str, "project": str, "status": "running"|"finished"|"failed",
  "epochs": int|None, "steps_per_epoch": int|None,
  "epoch": int, "batch": int, "global_step": int,
  "metrics": {name: float},          # latest scalar values logged
  "history": {name: [float, ...]},   # last ~120 values per metric, for trends
  "started_at": float, "updated_at": float,
}
```
Message dict: `{"text": str, "level": "info"|"warn"|"error", "until": float}`

### Views to implement (ENGINEER C)
- `SystemView`   — idle default: CPU% + per-core bars, GPU util/temp/VRAM (4090), RAM bar, clock. Cyberpunk/neon (reuse Canvas glow).
- `TrainingView` — headline metrics (loss/acc + any logged scalars), epoch progress bar filling with batch (shows `EPOCH n/N`, `batch b/B`), and a 128-cell SM-utilization heatmap driven by **real** `ctx.metrics["gpu"]["util"]` (spread across cells with per-cell jitter). Port look from `~/ttlcd-main/layouts.py::Train` + `claude_anim.py`.
- `MessageView`  — big centered card with the message text, colored by level.
- `MascotView`  — the Claude run-in/knock/wave/run-off loop. Port from `~/ttlcd-main/claude_anim.py::Claude` to the Canvas API.

Each view: `class XView(View)` with `def render(self, ctx) -> PIL.Image`. Keep per-view animation state on `self`. No hardware/USB access; pure rendering.

## ViewManager (LEAD)  — render priority
1. `message` present and not expired → MessageView (full-screen, ~5s default)
2. `run` present and status==running (or finished within last ~20s) → TrainingView
3. else idle → SystemView (configurable: mascot)
Manager owns the frame counter and builds `ctx`.

## HTTP API (LEAD, served by `server.py`) — default `127.0.0.1:8770`
- `GET  /health` → `{"ok":true,"version":str,"uptime_s":float,"panel":"running"|"down","gpu":bool}`
- `GET  /system` → Collector.snapshot()
- `POST /message` `{text, duration?=5, level?="info"}` → `{"ok":true}`
- `POST /run/start` `{project, epochs?, steps_per_epoch?, config?}` → `{"run_id":str}`
- `POST /run/log` `{run_id?, metrics:{}, epoch?, batch?, step?}` → `{"ok":true}`
- `POST /run/finish` `{run_id?, status?="finished"}` → `{"ok":true}`
- `GET  /run` → current run-state or `{}`
- `POST /view` `{name:"system"|"mascot"|"training"}` → force idle view
- `POST /issue` `{title, body, agent?}` → appends to `ISSUES.md`, `{"ok":true}`

## Panel SDK (ENGINEER B) — `from ttlcd_panel import Panel`
```python
Panel(project="x", epochs=None, steps_per_epoch=None,
      url="http://127.0.0.1:8770", quiet=True)
  .log(metrics: dict, epoch=None, batch=None, step=None)
  .message(text, duration=5, level="info")
  .finish(status="finished")
  # context manager: __enter__/__exit__ (auto start run + finish)
```
Resilience: if the daemon is unreachable, **warn once and become a no-op** — never
crash the caller's training loop. Timeouts ≤ 0.5s so logging never stalls training.

## Conventions
- Python ≥3.11, 4-space indent, type hints where cheap, stdlib `logging`.
- No network calls in render path. Collector caches; views read cached dict.
- Everything localhost-only by default.
