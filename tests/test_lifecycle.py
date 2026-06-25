"""Run-lifecycle hardening from the multi-agent audit:
abandoned-run eviction, no resurrection of finished runs, config sanitation,
and a metric-key cap."""
import json
import logging

import ttlcd_panel.manager as mgr
from ttlcd_panel.manager import ViewManager


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


def _by_id(m):
    return {r["run_id"]: r for r in m.get_runs()}


def test_finished_run_not_resurrected_by_late_log():
    m = _mgr()
    rid = m.start_run(project="p")
    m.finish_run(status="finished", run_id=rid)
    m.log_run({"loss": 0.5}, run_id=rid)        # late log after finish
    r = _by_id(m)[rid]
    assert r["status"] == "finished"
    assert "loss" not in r["metrics"]


def test_config_nan_inf_sanitized_and_json_safe():
    m = _mgr()
    m.start_run(project="p", config={"lr": float("nan"),
                                     "nested": {"x": float("inf")}, "ok": 0.1})
    cfg = m.get_runs()[0]["config"]
    assert cfg["lr"] is None and cfg["nested"]["x"] is None and cfg["ok"] == 0.1
    json.dumps(m.get_runs())                     # must not raise (no NaN/Inf left)


def test_abandoned_run_is_evicted_after_stale_window(monkeypatch):
    m = _mgr()
    t = [1000.0]
    monkeypatch.setattr(mgr.time, "time", lambda: t[0])
    rid = m.start_run(project="p")
    m.log_run({"loss": 1.0}, run_id=rid)
    assert rid in _by_id(m)                        # present while fresh
    t[0] += mgr.STALE_S + 1                         # client died, never finished
    m.render()                                     # render() runs _expire()
    assert m.get_runs() == []                       # abandoned run evicted


def test_metric_key_count_is_capped():
    m = _mgr()
    rid = m.start_run(project="p")
    for i in range(mgr.MAX_METRIC_KEYS + 30):
        m.log_run({f"m{i}": float(i)}, run_id=rid)
    assert len(_by_id(m)[rid]["metrics"]) == mgr.MAX_METRIC_KEYS
