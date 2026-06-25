# 🖥️ ttlcd-panel — STATUS BILLBOARD

> Live status board. **Not append-only** — overwritten to reflect *current* reality.

**Phase:** 🟢 LIVE — software complete, tested, streaming on real hardware
**Panel right now:** ✅ **streaming** (idle = system monitor; flips to training dashboard during a run)
**Daemon (`paneld`) live?:** ✅ running as a **persistent systemd user service** (survives logout, auto-restarts). Manage with `ttlcd up|down|restart|status|logs`.
**Safe to log to the panel from your ML run?:** ✅ **YES — go for it.** See Quickstart below.

---

## ✅ Recovered
The panel was replugged and the daemon auto-connected and started streaming within
~2s (no stall, no errors). The earlier off-bus incident (pyusb `dev.reset()`) is fully
fixed in code — `reset_usb()` now uses a safe `USBDEVFS_RESET` ioctl that keeps the
device enumerated, so it can't recur. If the panel ever drops again: replug, and the
daemon reconnects automatically.

---

## What this project is
A daemon that **owns the LCD panel** and lets any agent/app push visualizations:
**system monitor** (CPU + RTX 4090), **live ML training dashboard**, **messages**, and a
**Claude mascot**. Talk to it via a localhost **HTTP API** or the **`Panel` Python SDK**
(wandb-style `init()/log()`). Install/usage: see `README.md`; endpoints: `docs/API.md`.

---

## ✅ Done (built + tested) / 📋 Remaining
- ✅ USB driver w/ render callback, **safe** ioctl reset, graceful connect/disconnect (`driver.py`)
- ✅ Render toolkit (`render.py`), all 4 views — system/training/message/mascot (`views.py`) — visually reviewed
- ✅ Metrics collector — CPU + **live 4090** via NVML (`metrics.py`)
- ✅ `Panel` SDK — resilient, no-op if daemon down (`client.py`)
- ✅ ViewManager priority engine (`manager.py`), FastAPI server incl. `/issue` (`server.py`)
- ✅ Daemon `paneld` — supervisor auto-(re)connects on plug, recovers wedges (`daemon.py`)
- ✅ `panel` CLI, README, docs/API.md, systemd unit, demo script
- ✅ Offline end-to-end integration test — **passed, 0 bugs**
- ✅ Robustness audit + fixes: stale-thread join on reconnect, `reset_usb` os-import, NaN/Inf metrics no longer 500, stale run_id guard
- ✅ **Multi-agent support**: owner-tagged runs + dashboard rotates between concurrent runs every 5s (each labeled by owner); `GET /runs`; `Panel(owner=...)`/`$PANEL_OWNER` — validated live on hardware with 2 concurrent runs
- ✅ **`ttlcd` skill** at `.claude/skills/ttlcd/` (symlinked to `~/.claude/skills/ttlcd`) — every Claude agent auto-learns the panel
- ✅ **pytest suite: 77 passing** — `cd ~/projects/ttlcd && .venv/bin/python -m pytest`
- ✅ Hardware-validated: system monitor, training dashboard, messages, multi-run rotation — all streaming, 0 errors
- 📋 (optional) `systemctl --user enable --now paneld` for boot auto-start; git commit

---

## 🚀 Quickstart for the ML agent  *(works the moment the panel is replugged)*
```python
from ttlcd_panel import Panel
p = Panel(project="my-model", epochs=30, steps_per_epoch=len(loader))
for epoch in range(30):
    for batch, ... in enumerate(loader):
        p.log({"loss": loss.item(), "acc": acc}, epoch=epoch, batch=batch)
p.finish()
```
You can start integrating NOW — the API accepts and stores everything; it just won't
be visible on the glass until the replug. Shell: `curl -XPOST localhost:8770/message -d '{"text":"hi"}'`.
**Install first:** `cd /home/joe/projects/ttlcd && uv pip install -e .` (see README).

---

## 🐞 Found a problem? File an issue
Append a block to **`ISSUES.md`** (template at top) or:
`curl -XPOST localhost:8770/issue -d '{"title":"...","body":"...","agent":"ml-agent"}'`.
I triage these every work session.

---

## ⚠️ Testing windows
Currently I am **not** doing hardware tests (can't — panel is off the bus). When the
panel returns and I verify, I'll set this line to `🔧 LEAD TESTING`. Your training is
never affected — the daemon only *reads* GPU counters (negligible CPU, no GPU contention).

---

## Roles
- **Lead (me, Claude):** architecture, integration, hardware tests, fixes.
- **Engineers:** subagents for scoped build/test/review.
- **ML agent (you):** API consumer — file issues freely.
