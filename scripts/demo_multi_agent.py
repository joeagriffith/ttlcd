"""Demo: two agents training concurrently, sharing the panel via rotation.

Simulates two independent training runs (different owners/projects) logging at
the same time. The daemon keeps both and rotates the dashboard between them
every ~5s, tagging each with its owner — so neither clobbers the other. Each
ends on the celebratory "COMPLETE" outcome screen.

Run (daemon must be up — `ttlcd up`):
    .venv/bin/python scripts/demo_multi_agent.py
    .venv/bin/python scripts/demo_multi_agent.py --epochs 8 --batches 40 --sleep 0.2

Safe to run without the daemon: the SDK no-ops.
"""
import argparse
import math
import random
import threading
import time

from ttlcd_panel import Panel


def _train(owner, project, epochs, bpe, seed, sleep, url):
    rng = random.Random(seed)
    p = Panel(project=project, epochs=epochs, steps_per_epoch=bpe, owner=owner, url=url)
    total = epochs * bpe
    step = 0
    for e in range(epochs):
        for b in range(bpe):
            step += 1
            prog = step / total
            loss = max(0.02, 2.2 * math.exp(-3 * prog) + 0.05 + rng.uniform(-0.02, 0.02))
            acc = min(0.99, 0.98 * (1 - math.exp(-3.3 * prog)) + rng.uniform(-0.01, 0.01))
            p.log({"loss": loss, "acc": acc}, epoch=e, batch=b)
            time.sleep(sleep)
    p.finish()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--batches", type=int, default=55)
    ap.add_argument("--sleep", type=float, default=0.16)
    ap.add_argument("--url", default="http://127.0.0.1:8770")
    args = ap.parse_args()

    agents = [("ml-agent-A", "resnet50", 1), ("ml-agent-B", "vit-base", 7)]
    threads = []
    for owner, project, seed in agents:
        t = threading.Thread(target=_train,
                             args=(owner, project, args.epochs, args.batches, seed, args.sleep, args.url))
        t.start()
        threads.append(t)
        time.sleep(0.4)
    for t in threads:
        t.join()
    print("both runs complete")


if __name__ == "__main__":
    main()
