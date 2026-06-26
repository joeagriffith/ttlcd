# Architecture

Internals guide for contributors to `ttlcd-panel`. For the HTTP surface, see
[`API.md`](API.md); for usage, see the top-level `README.md`.

## Overview

`ttlcd-panel` drives a Thermaltake 3.9" Bar-Type TFT LCD (USB `264a:233d`,
**480x128** RGB) as a live, animated dashboard for system metrics, ML training
runs, messages, and a mascot animation.

The central design principle is **single ownership of the panel**. The USB
device is single-process by design (it relies on module-level init state), so
exactly one process — the daemon (`paneld`) — opens it, streams frames to it,
and supervises it. Everything else is a *client*: training scripts, agents, and
the CLI talk to the daemon over a localhost HTTP API and never touch USB. This
keeps the fragile init handshake in one place and lets many producers share one
panel without clobbering the bus.

## Component map (`src/ttlcd_panel/`)

- **`driver.py`** — low-level USB driver. `LcdDriver` owns the device and spins
  up the streaming threads; pulls each frame from a `render_fn` callback.
- **`render.py`** — the rendering toolkit: cached `font()`, `hsv()`, and the
  `Canvas` drawing primitives (gradients, bars, glow, text) shared by all views.
- **`metrics.py`** — `Collector`, a background poller that caches a system + GPU
  snapshot. `snapshot()` never blocks or raises.
- **`views.py`** — `View` subclasses, each `render(ctx) -> PIL.Image`. Pure
  rendering: no USB, no network, animation state kept on `self`.
- **`manager.py`** — `ViewManager`, the brain between API and panel. Holds run /
  message state, picks the active view, builds `ctx`, drives multi-run rotation.
- **`server.py`** — thin FastAPI app (`create_app`) over the manager + collector:
  validate, delegate, return JSON.
- **`daemon.py`** — `paneld` entry point. Wires collector + manager + server and
  runs `PanelService`, the supervisor that keeps the panel connected.
- **`client.py`** — `Panel`, the resilient wandb-style HTTP SDK clients import.
- **`cli.py`** — the `panel` command (message, status, system, run, view, issue).

## Data flow: producing a frame

The driver streams continuously (a few fps). For each frame:

1. The driver's `Main` thread calls the render callback, which is
   `ViewManager.render` (wired in `PanelService._build`).
2. `render()` takes its lock, increments the frame counter, expires stale
   message/run state, selects the active view, and builds a read-only `ctx`
   snapshot (`frame`, `t`, `metrics`, `run`, `message`). It releases the lock
   *before* drawing, so API mutations never block on rendering.
3. The selected view draws a `Canvas` from `ctx` and returns a 480x128
   `PIL.Image`.
4. The driver normalizes size/orientation, saves the frame as JPEG, and
   packetizes it over the USB bulk endpoint in 1020-byte chunks.

If a view raises, `render()` logs and returns `None`; the driver substitutes a
black frame rather than crashing the stream.

### Metrics caching

`Collector.start()` spawns a daemon thread that polls every `interval` (default
1 s): CPU via `psutil` (non-blocking, primed at start), RAM, network throughput
(byte-delta to MB/s), and GPU. GPU uses `pynvml` if available, else parses
`nvidia-smi`, else reports `None`. Each poll replaces a single cached dict under
a lock. Views and `GET /system` read that cache via `snapshot()`, which only
copies the reference — it never polls, blocks, or raises, so the render path and
API stay fast regardless of how slow a probe is.

## View selection

`ViewManager._pick_view()` (called by `render()` under the lock) resolves the
active view by priority:

1. **message** — an unexpired message → `MessageView` (full-screen card,
   default ~5 s).
2. **rotating displays** — one or more active runs and/or agendas → the manager
   rotates the slot between them (see below). A run renders as `TrainingView`
   (or `OutcomeView` once finished/crashed); an agenda renders as `AgendaView`.
3. **idle** — otherwise the configured idle view, `SystemView` (default) or
   `MascotView` (`--idle`, or `POST /view`).

`_active_name()` exposes this category (`"message"` / `"training"` / `"agenda"`
/ idle name) for tests and introspection.

### Rotation: runs + agendas

Runs are keyed by `run_id` and tagged with an `owner`, so concurrent agents
coexist instead of overwriting one shared run. Agendas (agent to-do checklists)
are keyed by `owner` — a new `POST /agenda` for an owner replaces that owner's
agenda. `_select_display()` lines up every eligible run (`running`, or finished
within the grace window, sorted by start time) followed by every eligible agenda
(non-empty and refreshed within `AGENDA_STALE_S = 30 min`, sorted by owner), then
picks one slot by wall-clock time (`int(time.time() / rotate_secs) % n`, default
`rotate_secs = 5 s`). So a run dashboard and a checklist cycle on the same panel.

Runs and agendas share the slot **equally** — a deliberate choice (the feature
request asked the panel to "rotate between them"), so heavy agenda use reduces
each run's share of screen time. Empty agendas are excluded so they can't steal a
slot for a blank "no items" card. The selected payload is stamped with
`_rotation = [current, total]` counted **within its own kind** ("run X of N runs"
/ "agenda X of N agendas"), so a single live run never shows a misleading "1/2"
just because an agenda is also up. `_active_name()` names the active category by
delegating to the same `_pick_view()` path, so the rotation maths live in one place.

`GET /run` returns a run (via `_select_run()`, which rotates among runs only — its
`_rotation` matches the run badge); `GET /runs` returns all runs; `GET /agenda`
returns all (non-empty) agendas. Because the panel rotates across runs *and*
agendas, `GET /run` reports an active run, not necessarily the exact payload on
screen at that instant.

A run that has **finished or crashed** is kept on screen for a grace window
(`RUN_GRACE_S = 20 s`) before `_expire()` drops it, so its terminal state is
visible. `TrainingView` reflects the final status (FINISHED / FAILED), and
`OutcomeView` provides a dedicated COMPLETE / CRASHED summary card for the
outcome screen.

## Data contracts

These are the shapes that flow between components; contributors should treat
them as stable.

### `Collector.snapshot()`

```python
{
  "ts": float,
  "cpu": {"pct": float, "per_core": [float, ...], "freq_mhz": float, "load1": float},
  "ram": {"pct": float, "used_gb": float, "total_gb": float},
  "net": {"up_mbps": float, "down_mbps": float},
  "gpu": {                                  # or None when no GPU
    "present": True, "name": str, "util": float,   # util 0..100
    "mem_used_gb": float, "mem_total_gb": float, "mem_pct": float,
    "temp_c": float, "power_w": float, "fan_pct": float,
  } | None,
}
```

Before the first poll, fields carry zero/`None` fallbacks. `per_core` length is
stable across polls. The snapshot is never partial and never raises.

### Run-state dict

```python
{
  "run_id": str, "project": str, "owner": str,
  "status": "running" | "finished" | "failed",
  "epochs": int | None, "steps_per_epoch": int | None,
  "epoch": int, "batch": int, "global_step": int,
  "metrics": {name: float},          # latest scalar per name
  "history": {name: [float, ...]},   # last ~120 values per metric, for trends
  "config": dict,
  "started_at": float, "updated_at": float,
  # "_rotation": [current, total]    # added by _select_run for the active run
}
```

Non-finite metric values (NaN/Inf) are dropped on log so JSON encoding can't
500. `global_step` auto-increments per log unless an explicit `step` is given.

### Message dict

```python
{"text": str, "level": "info" | "warn" | "error", "until": float}
```

`until` is an absolute wall-clock expiry; `_expire()` clears it once passed.

### Agenda dict

```python
{
  "owner": str, "title": str,
  "items": [{"task": str, "status": "done" | "doing" | "todo"}, ...],
  "updated_at": float,
  # "_rotation": [current, total]   # added by _select_display for the active agenda
}
```

Keyed by `owner` (one agenda per owner). `set_agenda()` sanitizes items —
non-dict entries are dropped, unknown statuses coerce to `"todo"`, tasks are
stringified, and the list is capped at `MAX_AGENDA_ITEMS = 64`. `_expire()`
drops an agenda once `updated_at` is older than `AGENDA_STALE_S` (30 min).

### View `ctx`

A read-only `SimpleNamespace` passed to `View.render(ctx)`:

```python
ctx.frame    # int, increments every rendered frame
ctx.t        # float wall-clock seconds (animation / elapsed)
ctx.metrics  # dict from Collector.snapshot()
ctx.run      # run-state dict, or None (only set for the training/outcome view)
ctx.agenda   # agenda dict, or None (only set for the agenda view)
ctx.message  # message dict, or None
```

## Threading & USB notes

The driver mirrors the proven upstream streaming engine; its quirks are
deliberate and load-bearing.

- **Module-level init handshake.** `GLOBAL_INIT_LOCK`, `GLOBAL_STAT`, and
  `GLOBAL_RUNNING` (in `driver.py`) sequence a multi-step init across the
  `Control`, `Write`, `Read`, `Main`, and `Trigger` threads. Each thread gates
  its work on the shared lock counter advancing through `MAX_GLOBAL_INIT` (13)
  stages. Because this state is module-global, the driver is **single-device,
  single-process** — only the daemon may drive the panel. `setup()` calls
  `_reset_globals()`, and `stop()` *joins* every thread before returning so no
  stale thread mutates the globals after a fresh connect resets them.
- **Write-health counter.** `USBControl.write()` tracks `GLOBAL_WRITE_FAILS`,
  the count of *consecutive* failed frame-packet writes: every success resets it
  to 0, every failure increments it. `writes_healthy()` is True while it stays
  below `MAX_WRITE_FAILS` (30). This detects a "write-failure wedge" — the device
  stays enumerated and the threads keep running, but every frame write is
  rejected — which the init globals alone cannot see. `_reset_globals()` clears
  it on each fresh connect.
- **`dev.reset()` is forbidden.** On this panel, pyusb's `dev.reset()` drops the
  device off the bus and it will not re-enumerate without a physical replug. To
  clear a wedged-but-present device, `reset_usb()` issues the `USBDEVFS_RESET`
  ioctl (`0x5514`) on the device's `/dev/bus/usb/...` node, which resets it while
  keeping it enumerated.
- **Supervisor.** `PanelService` (in `daemon.py`) runs a watchdog loop:
  - *Connect* when the device is present: build driver, `setup()`, `start()`,
    and wait up to ~75 s for `GLOBAL_RUNNING` before declaring success.
  - *Disconnect* — when the device is absent from the bus it logs once and waits
    quietly, reconnecting the moment it reappears.
  - *Wedge* — present but not streaming: `stop()` the old driver, `reset_usb()`
    to clear the wedge, then reconnect. `is_streaming()` checks `GLOBAL_RUNNING`,
    a live `Main` thread, *and* `writes_healthy()` (so a write-failure wedge is
    treated as down and recovered); `status()` maps this to `running` / `down` /
    `disconnected` for `GET /health`.

## Extending it: adding a view

1. Subclass `View` in `views.py` and implement
   `render(self, ctx) -> PIL.Image`. Build a `render.Canvas`, draw from `ctx`
   only (no USB, no network), and return `canvas.finish()`. Keep any animation
   state on `self` and key it off `ctx.frame` / `ctx.t`.
2. Register the instance in `ViewManager.__init__`'s `self.views` dict under a
   name.
3. Make it reachable: extend `_active_name()` for a new priority, or add it to
   the idle-view choices (`set_idle_view`, the `--idle` arg, and `POST /view`).

Reuse the `Canvas` helpers (`gradient`, `grid`, `bar`, `text`/`rtext`, `line`,
`ellipse`, `arc`, `sparkle`, plus `glow=True` for the neon bloom) and the
`font()` / `hsv()` helpers for a look consistent with the existing views.
```
