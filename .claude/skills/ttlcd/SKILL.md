---
name: ttlcd
description: Display or visualize ML training progress, live metrics (loss/acc), or status messages on the physical Thermaltake 3.9" LCD panel attached to this machine. Use when an agent or training script wants to show a run dashboard, flash a message, or check the panel daemon on localhost:8770.
---

# ttlcd-panel

Drive the physical Thermaltake Bar LCD on this machine via a localhost daemon
(`paneld`) on `127.0.0.1:8770`. Show a wandb-style training dashboard, flash
messages, and rotate between multiple agents' concurrent runs.

## 1. Check the daemon is up

```bash
curl -s localhost:8770/health    # expect "panel":"running"
```

If it is not running, start it with `ttlcd up` (a persistent systemd service;
`ttlcd down|restart|status|logs` manage it). The SDK no-ops if the daemon is
down, so it never breaks training.

## 2. Log a training run

Always set a distinct `owner` (your agent identity). Concurrent runs from
different agents then coexist and the panel **rotates between them every 5s**,
tagged by owner — instead of clobbering each other.

### Python SDK

```python
from ttlcd_panel import Panel
p = Panel(project="resnet50", epochs=90, steps_per_epoch=1000, owner="my-agent")
for e in range(90):
    for b in range(1000):
        p.log({"loss": loss, "acc": acc}, epoch=e, batch=b)
p.finish()                       # or p.finish(status="failed")
```

`owner` defaults to `$PANEL_OWNER`, else `"agent"`. `Panel` also works as a
context manager (auto-finishes, marks `failed` on exception).

### Zero-install curl

```bash
# start -> returns {"run_id": "..."}
curl -s -XPOST localhost:8770/run/start \
  -d '{"project":"demo","epochs":5,"steps_per_epoch":40,"owner":"my-agent"}'

curl -s -XPOST localhost:8770/run/log \
  -d '{"metrics":{"loss":0.31,"acc":0.92},"epoch":1,"batch":17,"owner":"my-agent"}'

curl -s -XPOST localhost:8770/run/finish -d '{"status":"finished","owner":"my-agent"}'
```

`run_id`/`owner` are optional on log/finish — omitting `run_id` targets the
most-recent running run. Pass the returned `run_id` to be explicit.

## 3. Flash a message

```bash
curl -s -XPOST localhost:8770/message -d '{"text":"training complete","level":"info"}'
```

Or `p.message("checkpoint saved", level="info")`. Levels: `info` | `warn` | `error`.

## 4. See who else is running

```bash
curl -s localhost:8770/runs       # {"runs": [ {owner, project, run_id, status, epoch, batch, metrics, ...} ]}
```

## 5. File an issue if something's broken

```bash
curl -s -XPOST localhost:8770/issue \
  -d '{"title":"panel froze","body":"USB wedge after 2h","agent":"my-agent"}'
```

Or append to `/home/joe/projects/ttlcd/ISSUES.md`.

## Notes

- **Localhost-only**, no GPU contention — the daemon only READS GPU counters.
- The SDK swallows all errors and no-ops if the daemon is down; it never stalls
  or crashes your training loop.
- Install once: `uv pip install -e /home/joe/projects/ttlcd`.
- More: `/home/joe/projects/ttlcd/README.md` and `/home/joe/projects/ttlcd/docs/API.md`.
