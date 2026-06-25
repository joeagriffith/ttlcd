"""metrics.Collector schema + lifecycle tests (no panel; GPU optional)."""
from __future__ import annotations

import logging
import time

from ttlcd_panel.metrics import Collector, _empty_snapshot


_GPU_KEYS = {
    "present", "name", "util", "mem_used_gb", "mem_total_gb",
    "mem_pct", "temp_c", "power_w", "fan_pct",
}


def _assert_schema(snap: dict):
    assert isinstance(snap, dict)
    assert set(snap) == {"ts", "cpu", "ram", "net", "gpu"}
    assert isinstance(snap["ts"], float)

    cpu = snap["cpu"]
    assert set(cpu) == {"pct", "per_core", "freq_mhz", "load1"}
    assert isinstance(cpu["pct"], float)
    assert isinstance(cpu["per_core"], list)
    assert all(isinstance(c, float) for c in cpu["per_core"])
    assert isinstance(cpu["freq_mhz"], float)
    assert isinstance(cpu["load1"], float)

    ram = snap["ram"]
    assert set(ram) == {"pct", "used_gb", "total_gb"}
    assert all(isinstance(ram[k], float) for k in ram)

    net = snap["net"]
    assert set(net) == {"up_mbps", "down_mbps"}
    assert all(isinstance(net[k], float) for k in net)

    gpu = snap["gpu"]
    assert gpu is None or isinstance(gpu, dict)
    if isinstance(gpu, dict):
        assert set(gpu) == _GPU_KEYS
        assert gpu["present"] is True
        assert isinstance(gpu["name"], str)
        for k in _GPU_KEYS - {"present", "name"}:
            assert isinstance(gpu[k], float)


def test_empty_snapshot_schema():
    _assert_schema(_empty_snapshot())


def test_snapshot_before_start():
    c = Collector(interval=0.1)
    snap = c.snapshot()
    _assert_schema(snap)


def test_snapshot_never_raises():
    c = Collector(interval=0.1)
    # call many times; must never raise
    for _ in range(50):
        c.snapshot()


def test_snapshot_after_start_and_stop():
    c = Collector(interval=0.1)
    c.start()
    try:
        # wait for at least one real poll to land
        deadline = time.time() + 3.0
        while time.time() < deadline and c.snapshot()["ts"] == 0.0:
            time.sleep(0.05)
        snap = c.snapshot()
        _assert_schema(snap)
        # after a real poll, per_core should be populated on a real machine
        assert isinstance(snap["cpu"]["per_core"], list)
    finally:
        c.stop()
    # thread must be gone after stop()
    assert c._thread is None


def test_stop_is_idempotent_and_ends_thread():
    c = Collector(interval=0.1)
    c.start()
    t = c._thread
    assert t is not None and t.is_alive()
    c.stop()
    assert not t.is_alive()
    # second stop must not raise
    c.stop()


def test_double_start_does_not_spawn_two_threads():
    c = Collector(interval=0.1)
    c.start()
    t1 = c._thread
    c.start()  # should be a no-op since already alive
    assert c._thread is t1
    c.stop()


def test_gpu_present_or_none(caplog):
    """GPU may be present (4090) or absent — both are contract-valid."""
    with caplog.at_level(logging.WARNING):
        c = Collector(interval=0.1)
        c.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and c.snapshot()["ts"] == 0.0:
                time.sleep(0.05)
            snap = c.snapshot()
        finally:
            c.stop()
    _assert_schema(snap)
    # explicitly assert both branches are tolerated
    assert snap["gpu"] is None or snap["gpu"]["present"] is True
