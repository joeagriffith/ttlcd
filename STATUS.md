# 🖥️ ttlcd-panel — STATUS BILLBOARD

> Live status board. **Not append-only** — overwritten to reflect *current* reality.

**Phase:** 🟢 LIVE — software complete, tested, streaming on real hardware
**Panel right now:** ✅ **streaming** (idle = system monitor; flips to training dashboard during a run)
**Daemon (`paneld`) live?:** ✅ running as a **persistent systemd user service** (survives logout, auto-restarts). Manage with `ttlcd up|down|restart|status|logs`.
**Safe to log to the panel from your ML run?:** ✅ **YES — go for it.** See Quickstart below.

---

## ✅ Recovered
The earlier off-bus incident (pyusb `dev.reset()`) is fully fixed in code —
`reset_usb()` now uses a safe `USBDEVFS_RESET` ioctl that keeps the device enumerated,
so it can't recur. If the panel ever drops: replug, and the daemon reconnects
automatically (it did exactly that — streaming resumed within ~2s, no errors).

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
- ✅ **Outcome screens**: runs end on a clear ✓ COMPLETE / ✕ CRASHED card
- ✅ **pytest suite: 79 passing** — `cd ~/projects/ttlcd && .venv/bin/python -m pytest`
- ✅ Hardware-validated: system monitor, training dashboard, messages, multi-run rotation — all streaming, 0 errors

---

## 🚀 Quickstart for the ML agent  *(panel is live — logs appear on the glass immediately)*
```python
from ttlcd_panel import Panel
p = Panel(project="my-model", epochs=30, steps_per_epoch=len(loader))
for epoch in range(30):
    for batch, ... in enumerate(loader):
        p.log({"loss": loss.item(), "acc": acc}, epoch=epoch, batch=batch)
p.finish()
```
The panel is streaming now — anything you log shows up live on the glass.
Shell: `curl -XPOST localhost:8770/message -d '{"text":"hi"}'`.
**Install first:** `cd /home/joe/projects/ttlcd && uv pip install -e .` (see README).

---

## 🐞 Found a problem? File an issue
Append a block to **`ISSUES.md`** (template at top) or:
`curl -XPOST localhost:8770/issue -d '{"title":"...","body":"...","agent":"ml-agent"}'`.
I triage these every work session.

---

## ⚠️ Testing windows
The panel is **live and streaming**. If I run a hardware verification pass I'll flip
this line to `🔧 LEAD TESTING` so you know. Either way your training is never affected —
the daemon only *reads* GPU counters (negligible CPU, no GPU contention).

---

## Roles
- **Lead (me, Claude):** architecture, integration, hardware tests, fixes.
- **Engineers:** subagents for scoped build/test/review.
- **ML agent (you):** API consumer — file issues freely.
