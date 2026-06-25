"""Shared fixtures + fakes for the ttlcd-panel test suite.

All tests run WITHOUT the physical panel and WITHOUT touching USB hardware.
"""
from __future__ import annotations

import logging

import pytest


def contract_snapshot(gpu: bool = True) -> dict:
    """A Collector.snapshot()-shaped dict matching ARCHITECTURE.md exactly."""
    snap = {
        "ts": 123.0,
        "cpu": {"pct": 12.5, "per_core": [10.0, 20.0, 30.0, 40.0], "freq_mhz": 3200.0, "load1": 0.5},
        "ram": {"pct": 40.0, "used_gb": 12.0, "total_gb": 32.0},
        "net": {"up_mbps": 1.0, "down_mbps": 2.0},
        "gpu": None,
    }
    if gpu:
        snap["gpu"] = {
            "present": True,
            "name": "NVIDIA GeForce RTX 4090",
            "util": 55.0,
            "mem_used_gb": 8.0,
            "mem_total_gb": 24.0,
            "mem_pct": 33.3,
            "temp_c": 60.0,
            "power_w": 250.0,
            "fan_pct": 40.0,
        }
    return snap


class FakeCollector:
    """A stand-in for metrics.Collector: snapshot() returns a contract dict."""

    def __init__(self, gpu: bool = True):
        self._snap = contract_snapshot(gpu=gpu)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def snapshot(self) -> dict:
        return self._snap


@pytest.fixture
def fake_collector():
    return FakeCollector(gpu=True)


@pytest.fixture
def logger():
    log = logging.getLogger("ttlcd-test")
    log.addHandler(logging.NullHandler())
    return log
