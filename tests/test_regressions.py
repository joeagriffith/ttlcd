"""Regression tests for bugs found in the v0.1 robustness audit."""
import logging

import pytest
from fastapi.testclient import TestClient

from ttlcd_panel import driver as driver_mod
from ttlcd_panel.manager import ViewManager
from ttlcd_panel.server import create_app


class FakeCollector:
    def snapshot(self):
        return {
            "ts": 0.0,
            "cpu": {"pct": 0.0, "per_core": [0.0] * 8, "freq_mhz": 0.0, "load1": 0.0},
            "ram": {"pct": 0.0, "used_gb": 0.0, "total_gb": 0.0},
            "net": {"up_mbps": 0.0, "down_mbps": 0.0},
            "gpu": None,
        }


def _mgr():
    return ViewManager(FakeCollector(), logging.getLogger("test"))


# Bug 3: NaN/Inf metrics must not be stored (they'd make JSON encoding 500).
def test_nan_inf_metrics_skipped_and_run_serialises():
    m = _mgr()
    m.start_run(project="t")
    m.log_run({"loss": float("nan"), "acc": float("inf"), "lr": 0.01})
    run = m.get_run()
    assert "loss" not in run["metrics"]
    assert "acc" not in run["metrics"]
    assert run["metrics"]["lr"] == 0.01

    # Even after a NaN was logged, the API must still return 200 (not 500),
    # because the manager dropped the non-finite value before it reached JSON.
    app = create_app(m, FakeCollector(), lambda: "down", __import__("pathlib").Path("/tmp/_x_issues.md"))
    c = TestClient(app)
    assert c.get("/run").status_code == 200
    assert c.get("/system").status_code == 200


# Multi-run contract (supersedes v0.1 "Bug 4"): logging with an unknown
# run_id now CREATES a run with that id (handles daemon restart / logging
# before an explicit start), and does NOT touch the existing run.
def test_unknown_run_id_creates_separate_run():
    m = _mgr()
    rid = m.start_run(project="real")
    m.log_run({"loss": 1.0}, run_id="totally-different-id")
    runs = {r["run_id"]: r for r in m.get_runs()}
    # original run is untouched...
    assert "loss" not in runs[rid]["metrics"]
    # ...and a new run was created under the supplied id
    assert "totally-different-id" in runs
    assert runs["totally-different-id"]["metrics"]["loss"] == 1.0
    # matching id keeps updating the same run
    m.log_run({"loss": 0.5}, run_id=rid)
    runs = {r["run_id"]: r for r in m.get_runs()}
    assert runs[rid]["metrics"]["loss"] == 0.5


# Bug 2: driver module must import `os` (reset_usb used os.* -> silent NameError).
def test_driver_has_os_imported():
    assert hasattr(driver_mod, "os")
    # reset_usb on an absent device returns False without raising NameError
    d = driver_mod.LcdDriver(lambda: None, logging.getLogger("test"), "/tmp/_frame.jpg")
    assert d.reset_usb() in (True, False)
