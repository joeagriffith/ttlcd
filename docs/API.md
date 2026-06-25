# ttlcd-panel HTTP API

The daemon (`paneld`) serves this API over **localhost** at
`http://127.0.0.1:8770` by default (`--host` / `--port` to change). All bodies
are JSON. Shapes below are derived from `src/ttlcd_panel/server.py`.

Field types: `?` marks an optional request field; the value after `=` is the
default the server applies when the field is omitted.

---

## GET /health

Daemon + panel liveness.

**Request:** none.

**Response:**
```json
{
  "ok": true,
  "version": "0.1.0",
  "uptime_s": 12.3,
  "panel": "running",
  "gpu": true
}
```
- `panel` is one of `"running"`, `"down"`, `"disconnected"` (device absent from
  the USB bus), or `"disabled"` (when started with `--no-panel`).
- `gpu` is `true` when a GPU snapshot is available.

```bash
curl -s http://127.0.0.1:8770/health
```

---

## GET /system

The latest cached system/GPU snapshot from the collector (never blocks).

**Request:** none.

**Response:** a `Collector.snapshot()` dict:
```json
{
  "ts": 1719270000.0,
  "cpu": {"pct": 12.5, "per_core": [10.0, 15.0], "freq_mhz": 3600.0, "load1": 0.4},
  "ram": {"pct": 31.2, "used_gb": 10.0, "total_gb": 32.0},
  "net": {"up_mbps": 0.2, "down_mbps": 1.1},
  "gpu": {
    "present": true, "name": "NVIDIA GeForce RTX 4090", "util": 73.0,
    "mem_used_gb": 8.1, "mem_total_gb": 24.0, "mem_pct": 33.8,
    "temp_c": 61.0, "power_w": 210.0, "fan_pct": 45.0
  }
}
```
`gpu` is `null` when no GPU is present.

```bash
curl -s http://127.0.0.1:8770/system
```

---

## POST /message

Flash a full-screen message card on the panel.

**Request:**
```json
{"text": "hello panel", "duration?": 5.0, "level?": "info"}
```
- `text` (string, required)
- `duration` (number, = `5.0`) — seconds to display
- `level` (string, = `"info"`) — `"info"` | `"warn"` | `"error"` (drives color)

**Response:**
```json
{"ok": true}
```

```bash
curl -s -X POST http://127.0.0.1:8770/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello panel","duration":5,"level":"info"}'
```

---

## POST /run/start

Start a training run (the panel switches to the training dashboard).

**Request:**
```json
{"project?": "run", "epochs?": null, "steps_per_epoch?": null, "config?": null, "owner?": null}
```
- `project` (string, = `"run"`)
- `epochs` (int or null, = `null`) — drives the epoch progress bar
- `steps_per_epoch` (int or null, = `null`) — batches per epoch
- `config` (object or null, = `null`) — arbitrary hyperparameters
- `owner` (string or null, = `null` → `"agent"`) — identifies the agent that
  owns this run. The panel tags each run with its owner and **rotates** the
  dashboard between concurrently-active runs every 5s, so multiple agents
  coexist instead of clobbering one another. Set a distinct owner per agent.

**Response:**
```json
{"run_id": "a1b2c3d4"}
```

```bash
curl -s -X POST http://127.0.0.1:8770/run/start \
  -H 'Content-Type: application/json' \
  -d '{"project":"demo","epochs":5,"steps_per_epoch":40,"owner":"trainer-A"}'
```

---

## POST /run/log

Log scalar metrics for the current (or a specified) run.

**Request:**
```json
{"run_id?": null, "metrics?": {}, "epoch?": null, "batch?": null, "step?": null, "owner?": null}
```
- `run_id` (string or null, = `null`) — targets the active run if omitted
- `metrics` (object of `name: float`, = `{}`)
- `epoch` (int or null, = `null`)
- `batch` (int or null, = `null`)
- `step` (int or null, = `null`)
- `owner` (string or null, = `null`) — tags/back-fills the owner of the run
  being logged (used for the multi-run rotation labelling)

**Response:**
```json
{"ok": true}
```

```bash
curl -s -X POST http://127.0.0.1:8770/run/log \
  -H 'Content-Type: application/json' \
  -d '{"metrics":{"loss":0.31,"acc":0.92},"epoch":1,"batch":17,"owner":"trainer-A"}'
```

---

## POST /run/finish

Mark the run finished or failed.

**Request:**
```json
{"run_id?": null, "status?": "finished", "owner?": null}
```
- `run_id` (string or null, = `null`) — active run if omitted
- `status` (string, = `"finished"`) — typically `"finished"` or `"failed"`
- `owner` (string or null, = `null`) — accepted for symmetry with the other
  run endpoints; identifies the calling agent

**Response:**
```json
{"ok": true}
```

```bash
curl -s -X POST http://127.0.0.1:8770/run/finish \
  -H 'Content-Type: application/json' -d '{"status":"finished","owner":"trainer-A"}'
```

---

## GET /run

The current run-state dict, or `{}` when no run is active.

**Request:** none.

**Response (active run):**
```json
{
  "run_id": "a1b2c3d4", "project": "demo", "status": "running",
  "epochs": 5, "steps_per_epoch": 40,
  "epoch": 1, "batch": 17, "global_step": 57,
  "metrics": {"loss": 0.31, "acc": 0.92},
  "history": {"loss": [0.9, 0.6, 0.31], "acc": [0.4, 0.7, 0.92]},
  "started_at": 1719270000.0, "updated_at": 1719270012.0
}
```
**Response (no run):**
```json
{}
```

```bash
curl -s http://127.0.0.1:8770/run
```

> Note: when several runs are active, `GET /run` returns the one currently on
> screen (the daemon rotates the dashboard between active runs every 5s). Use
> `GET /runs` to see all of them.

---

## GET /runs

All currently-active runs (running, plus those finished within the grace
window). The daemon rotates the training dashboard between these every 5s, each
labelled by its `owner`, so multiple agents can log concurrently.

**Request:** none.

**Response:**
```json
{
  "runs": [
    {
      "run_id": "a1b2c3d4e5f6", "project": "demo", "owner": "trainer-A",
      "status": "running", "epochs": 5, "steps_per_epoch": 40,
      "epoch": 1, "batch": 17, "global_step": 57,
      "metrics": {"loss": 0.31, "acc": 0.92},
      "history": {"loss": [0.9, 0.6, 0.31], "acc": [0.4, 0.7, 0.92]},
      "config": {},
      "started_at": 1719270000.0, "updated_at": 1719270012.0
    }
  ]
}
```
`runs` is `[]` when nothing is active. Runs are sorted by start time (the
stable rotation order).

```bash
curl -s http://127.0.0.1:8770/runs
```

---

## POST /view

Force the idle view.

**Request:**
```json
{"name": "system"}
```
- `name` (string, required) — `"system"` | `"mascot"`. These are the only
  settable idle views.

**Response:**
```json
{"ok": true}
```
`ok` is `false` if the view name was not recognized.

```bash
curl -s -X POST http://127.0.0.1:8770/view \
  -H 'Content-Type: application/json' -d '{"name":"mascot"}'
```

---

## POST /issue

Append an issue to `ISSUES.md` (used by agents to file problems to the lead).

**Request:**
```json
{"title": "panel froze", "body?": "", "agent?": "unknown", "severity?": "medium"}
```
- `title` (string, required)
- `body` (string, = `""`)
- `agent` (string, = `"unknown"`)
- `severity` (string, = `"medium"`)

**Response:**
```json
{"ok": true}
```

```bash
curl -s -X POST http://127.0.0.1:8770/issue \
  -H 'Content-Type: application/json' \
  -d '{"title":"panel froze","body":"USB wedge after 2h","agent":"trainer","severity":"high"}'
```
