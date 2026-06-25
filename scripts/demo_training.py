#!/usr/bin/env python3
"""Fake-training demo for the ttlcd-panel SDK.

Simulates a short training run (decreasing loss, rising accuracy with noise)
and streams it to the LCD panel via the wandb-style ``Panel`` SDK so you can
watch the training dashboard animate.

This is safe to run whether or not the daemon is up: if ``paneld`` isn't
running, the SDK warns once and no-ops, so the loop still completes locally.

Run it:

    # 1. (optional) start the daemon in another terminal so the panel animates:
    .venv/bin/paneld

    # 2. run the demo:
    .venv/bin/python scripts/demo_training.py

Useful flags:

    --epochs N        number of epochs (default 5)
    --batches N       batches per epoch (default 40)
    --sleep S         seconds to sleep per batch (default 0.05, for animation)
    --url URL         daemon URL (default http://127.0.0.1:8770)
    --dry             run a single fast batch with no sleep (smoke test)
"""

from __future__ import annotations

import argparse
import math
import random
import time

from ttlcd_panel import Panel


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="demo_training.py",
        description="Fake-training demo that drives the ttlcd-panel SDK.",
    )
    p.add_argument("--epochs", type=int, default=5, help="number of epochs")
    p.add_argument("--batches", type=int, default=40, help="batches per epoch")
    p.add_argument("--sleep", type=float, default=0.05,
                   help="seconds to sleep per batch (animation pacing)")
    p.add_argument("--url", default="http://127.0.0.1:8770", help="daemon URL")
    p.add_argument("--dry", action="store_true",
                   help="single fast batch, no sleep (smoke test)")
    return p.parse_args(argv)


def fake_metrics(progress: float) -> tuple[float, float]:
    """Return (loss, acc) for a fractional progress in [0, 1], with noise."""
    loss = 2.5 * math.exp(-3.0 * progress) + random.uniform(-0.04, 0.04)
    acc = (1.0 - math.exp(-3.5 * progress)) + random.uniform(-0.02, 0.02)
    return max(0.0, loss), min(1.0, max(0.0, acc))


def main(argv=None) -> int:
    args = parse_args(argv)

    epochs = 1 if args.dry else args.epochs
    batches = 1 if args.dry else args.batches
    sleep = 0.0 if args.dry else args.sleep
    total = max(1, epochs * batches)

    # Constructing the Panel starts a run on the daemon (or no-ops if it's down).
    with Panel(
        project="demo-training",
        epochs=epochs,
        steps_per_epoch=batches,
        url=args.url,
        quiet=False,  # show the "daemon unreachable" warning once if down
    ) as p:
        p.message("starting demo run 🚀", level="info")
        step = 0
        for epoch in range(epochs):
            for batch in range(batches):
                progress = (step + 1) / total
                loss, acc = fake_metrics(progress)
                p.log(
                    {"loss": round(loss, 4), "acc": round(acc, 4)},
                    epoch=epoch,
                    batch=batch,
                    step=step,
                )
                step += 1
                if sleep:
                    time.sleep(sleep)
        p.message("training complete ✅", level="info")

    # __exit__ calls p.finish() for us.
    print(f"done: {epochs} epochs x {batches} batches = {step} steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
