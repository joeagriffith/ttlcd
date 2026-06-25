"""Outcome screens: a finished run shows the COMPLETE screen, a failed run the
CRASHED screen — routed by the ViewManager during the run's grace window."""
import logging

from PIL import Image

from ttlcd_panel.manager import ViewManager
from ttlcd_panel.views import OutcomeView


class FakeCollector:
    def snapshot(self):
        return {
            "ts": 0.0,
            "cpu": {"pct": 0.0, "per_core": [0.0] * 8, "freq_mhz": 0.0, "load1": 0.0},
            "ram": {"pct": 0.0, "used_gb": 0.0, "total_gb": 0.0},
            "net": {"up_mbps": 0.0, "down_mbps": 0.0},
            "gpu": None,
        }


class Spy:
    """A stand-in view that records whether it was asked to render."""
    def __init__(self):
        self.calls = 0

    def render(self, ctx):
        self.calls += 1
        return Image.new("RGB", (480, 128), (0, 0, 0))


def _mgr():
    return ViewManager(FakeCollector(), logging.getLogger("test"))


def test_outcomeview_renders_finished_and_failed():
    v = OutcomeView()
    for status in ("finished", "failed"):
        run = {
            "status": status, "project": "demo", "owner": "agent",
            "epoch": 4, "epochs": 5, "metrics": {"loss": 0.3, "acc": 0.9},
            "started_at": 1000.0, "updated_at": 1090.0,
        }
        from types import SimpleNamespace
        img = v.render(SimpleNamespace(frame=3, t=0.0, metrics={}, run=run, message=None))
        assert img.size == (480, 128) and img.mode == "RGB"


def test_manager_routes_finished_run_to_outcome_view():
    m = _mgr()
    train, out = Spy(), Spy()
    m.views["training"], m.views["outcome"] = train, out

    rid = m.start_run(project="p", owner="a")
    m.log_run({"loss": 1.0}, run_id=rid)
    m.render()
    assert train.calls == 1 and out.calls == 0   # running -> training dashboard

    m.finish_run(status="finished", run_id=rid)
    m.render()
    assert out.calls == 1                          # finished -> outcome screen

    m.finish_run(status="failed", run_id=rid)
    m.render()
    assert out.calls == 2                          # failed -> outcome screen too
