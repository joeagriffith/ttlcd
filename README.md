# ttlcd-panel

A daemon + Python SDK that turns a **Thermaltake 3.9" Bar LCD** (USB
`264a:233d`, **480x128** RGB) into a live status display: system/GPU stats when
idle, a wandb-style **ML training dashboard** while a run is active, full-screen
**messages**, and an animated Claude **mascot**. A long-lived daemon (`paneld`)
owns the panel and serves a small localhost HTTP API; your training code (or any
script) talks to it through the resilient `Panel` SDK — if the daemon is down,
the SDK silently no-ops so it never crashes your loop.

---

## Install

This machine uses **[uv](https://docs.astral.sh/uv/)**. From the project root:

```bash
cd /home/joe/projects/ttlcd
uv venv                      # create .venv (Python >=3.11)
uv pip install -e .          # install ttlcd-panel + deps (editable)
```

This installs two console scripts into `.venv/bin/`: `paneld` (the daemon) and
`panel` (the CLI).

### One-time udev rule (non-root USB access)

The daemon talks to the panel over raw USB, which normally requires root. A
udev rule grants user-level access by setting mode `0666` on the device. The
rule already exists at `/etc/udev/rules.d/99-ttlcd.rules`; to (re)create it:

```bash
sudo tee /etc/udev/rules.d/99-ttlcd.rules >/dev/null <<'EOF'
# Thermaltake 3.9" Bar LCD — allow non-root access
SUBSYSTEM=="usb", ATTRS{idVendor}=="264a", ATTRS{idProduct}=="233d", MODE="0666"
EOF

sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug/replug the panel (or reboot) after creating the rule so it takes effect.

---

## Run the daemon

Easiest — the `ttlcd` lifecycle CLI (installs a systemd **user** service, so it
survives logout and auto-restarts on crash):

```bash
ttlcd up        # install + start            ttlcd status   # service + panel health
ttlcd down      # stop                       ttlcd logs -f  # follow logs
ttlcd restart   # restart (alias: reload)
```

`ttlcd` lives at `bin/ttlcd` (symlink it onto your PATH: `ln -sf "$PWD/bin/ttlcd" ~/.local/bin/ttlcd`).
For boot-without-login, also run `loginctl enable-linger $USER` (needs sudo).

Or run it directly in the foreground:

```bash
.venv/bin/paneld
```

Useful flags (some have env-var equivalents — see `paneld --help`):

| Flag | Default | Purpose |
|------|---------|---------|
| `--host` | `127.0.0.1` | API bind address (localhost-only by default) |
| `--port` | `8770` | API port |
| `--idle` | `system` | Idle view: `system` stats or `mascot` (Claude animation) |
| `--orientation` | `top` | Physical mounting orientation of the panel |
| `--no-panel` | off | Run the **API only**, never touch USB (dev/testing) |
| `--interval` | `1.0` | Metrics poll interval, seconds |
| `--rotate` | `5` | Seconds per run when rotating the dashboard across concurrent runs |
| `--log-level` | `INFO` | Logging level |

For headless / always-on operation, install it as a user service — see
[`packaging/paneld.service`](packaging/paneld.service).

---

## Usage

### Panel SDK (wandb-style)

```python
from ttlcd_panel import Panel

# Constructing a Panel immediately starts a run on the daemon.
panel = Panel(project="resnet50", epochs=90, steps_per_epoch=1000)
for epoch in range(90):
    for batch in range(1000):
        loss, acc = train_step()
        panel.log({"loss": loss, "acc": acc}, epoch=epoch, batch=batch)
panel.message("training complete ✅")
panel.finish()
```

As a **context manager** (auto-starts the run, auto-finishes — and marks the run
`failed` if an exception propagates):

```python
from ttlcd_panel import Panel

with Panel(project="quick", epochs=3) as panel:
    panel.message("starting up!")
    panel.log({"acc": 0.99}, step=42)
```

The SDK is deliberately resilient: every call uses a short (<=0.5s) timeout and
swallows all errors. If the daemon is unreachable it warns once (unless
`quiet=True`, the default) and becomes a silent no-op — logging never stalls or
crashes your training loop.

A runnable end-to-end demo lives at
[`scripts/demo_training.py`](scripts/demo_training.py):

```bash
.venv/bin/python scripts/demo_training.py
```

### Multiple agents

Several agents can log to the panel at once. Pass a distinct `owner=` to each
`Panel` (or an `owner` field on the `/run/*` requests) — it defaults to
`$PANEL_OWNER`, else `"agent"`:

```python
panel = Panel(project="resnet50", epochs=90, owner="trainer-A")
```

The daemon keeps every active run and **rotates the training dashboard between
them every 5 seconds**, tagging each with its owner, so concurrent runs coexist
instead of clobbering one another. See all active runs with
`curl -s http://127.0.0.1:8770/runs`.

Claude agents on this machine get a ready-made guide via the
[`ttlcd` skill](.claude/skills/ttlcd/SKILL.md).

### One-off message (CLI)

```bash
.venv/bin/panel msg "hi"                      # flash a message
.venv/bin/panel msg "build failed" --level error --duration 8
.venv/bin/panel status                        # daemon + panel health
.venv/bin/panel system                        # latest system/GPU snapshot
.venv/bin/panel run                           # current run state
.venv/bin/panel view mascot                   # switch idle view
```

### HTTP API (curl)

Full reference: [`docs/API.md`](docs/API.md). Quick examples:

```bash
# Health
curl -s http://127.0.0.1:8770/health

# System / GPU snapshot
curl -s http://127.0.0.1:8770/system

# Flash a message
curl -s -X POST http://127.0.0.1:8770/message \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello panel","duration":5,"level":"info"}'

# Start a run, log to it, finish it
curl -s -X POST http://127.0.0.1:8770/run/start \
  -H 'Content-Type: application/json' \
  -d '{"project":"demo","epochs":5,"steps_per_epoch":40}'

curl -s -X POST http://127.0.0.1:8770/run/log \
  -H 'Content-Type: application/json' \
  -d '{"metrics":{"loss":0.31,"acc":0.92},"epoch":1,"batch":17}'

curl -s -X POST http://127.0.0.1:8770/run/finish \
  -H 'Content-Type: application/json' -d '{"status":"finished"}'
```

---

## How it works

- **The daemon owns the panel.** `paneld` is the only process that touches the
  USB device; it renders frames (a few fps) and serves the HTTP API.
- **A collector polls the machine.** A background thread samples CPU, RAM, net,
  and GPU (via `pynvml`, falling back to `nvidia-smi`) on an interval and caches
  the latest snapshot — the render path never does any I/O.
- **A view manager picks what to show**, by priority:
  **message** (full-screen card, ~5s) > **run** (training dashboard while a run
  is active / recently finished) > **idle** (system stats or mascot).
- **A watchdog auto-recovers USB wedges.** If the panel's render thread dies, the
  daemon resets the USB device and re-initialises the panel; the API stays up the
  whole time.

---

## More

- Current build state: [`STATUS.md`](STATUS.md)
- Known issues / bug log: [`ISSUES.md`](ISSUES.md)
- Internals & module contracts: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
