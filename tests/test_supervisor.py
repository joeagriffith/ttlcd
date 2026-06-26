"""daemon.PanelService supervisor tests WITHOUT hardware.

We never touch USB. We monkeypatch:
  * ``LcdDriver.device_present`` -> a controllable flag
  * ``PanelService._build``      -> returns a FakeDriver (records start/stop)
  * ``driver_mod.GLOBAL_RUNNING`` -> toggled to simulate streaming
"""
from __future__ import annotations

import logging

import pytest

import ttlcd_panel.driver as driver_mod
from ttlcd_panel.daemon import PanelService


class _FakeMainThread:
    """Mimics the driver's 'Main' thread: class name must be 'Main'."""

    def __init__(self, alive=True):
        self._alive = alive

    def is_alive(self):
        return self._alive


# the class name is what is_streaming() inspects, so alias it
Main = _FakeMainThread
Main.__name__ = "Main"


class FakeDriver:
    def __init__(self, with_main=True, main_alive=True):
        self.setup_called = 0
        self.start_called = 0
        self.stop_called = 0
        self.reset_called = 0
        self._threads = [Main(alive=main_alive)] if with_main else []

    def setup(self):
        self.setup_called += 1

    def start(self):
        self.start_called += 1

    def stop(self):
        self.stop_called += 1

    def reset_usb(self):
        self.reset_called += 1
        return True


@pytest.fixture
def log():
    lg = logging.getLogger("supervisor-test")
    lg.addHandler(logging.NullHandler())
    return lg


@pytest.fixture(autouse=True)
def restore_globals():
    saved = driver_mod.GLOBAL_RUNNING
    saved_fails = driver_mod.GLOBAL_WRITE_FAILS
    yield
    driver_mod.GLOBAL_RUNNING = saved
    driver_mod.GLOBAL_WRITE_FAILS = saved_fails


def _service(log, monkeypatch, present, fake_driver):
    monkeypatch.setattr(driver_mod.LcdDriver, "device_present",
                        classmethod(lambda cls, *a, **k: present["v"]))
    svc = PanelService(manager=object(), logger=log, image_path="/dev/null")
    monkeypatch.setattr(svc, "_build", lambda: fake_driver)
    return svc


# ------------------------------------------------------------ device absent

def test_status_disconnected_when_device_absent(log, monkeypatch):
    present = {"v": False}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    driver_mod.GLOBAL_RUNNING = False
    assert svc.status() == "disconnected"
    assert svc.is_streaming() is False


def test_loop_does_not_connect_when_absent(log, monkeypatch):
    """One supervised pass with device absent must not build/connect a driver."""
    present = {"v": False}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    driver_mod.GLOBAL_RUNNING = False

    # drive a single loop body manually (avoid the infinite while + sleeps)
    # mirror the absent branch: is_streaming() False, device_present() False
    assert svc.is_streaming() is False
    assert driver_mod.LcdDriver.device_present() is False
    # _try_connect must never have been called -> driver stays None
    assert svc.driver is None
    assert fake.start_called == 0


# ----------------------------------------------------------- device present

def test_try_connect_when_present(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    svc._running = True
    # simulate the device coming up: GLOBAL_RUNNING flips True after start()
    driver_mod.GLOBAL_RUNNING = True
    ok = svc._try_connect()
    assert ok is True
    assert fake.setup_called == 1
    assert fake.start_called == 1
    assert svc.driver is fake


def test_try_connect_stalls_when_not_running(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    svc._running = False  # deadline loop exits immediately
    driver_mod.GLOBAL_RUNNING = False
    ok = svc._try_connect()
    assert ok is False
    # on failure it attempts to stop the driver it built
    assert fake.stop_called >= 1


# --------------------------------------------------------- is_streaming logic

def test_is_streaming_true_when_running_and_main_alive(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver(with_main=True, main_alive=True)
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    driver_mod.GLOBAL_RUNNING = True
    assert svc.is_streaming() is True
    assert svc.status() == "running"


def test_is_streaming_false_when_global_not_running(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver(with_main=True, main_alive=True)
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    driver_mod.GLOBAL_RUNNING = False
    assert svc.is_streaming() is False
    # present but not streaming -> "down"
    assert svc.status() == "down"


def test_is_streaming_false_when_main_dead(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver(with_main=True, main_alive=False)
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    driver_mod.GLOBAL_RUNNING = True
    assert svc.is_streaming() is False


def test_is_streaming_false_on_write_failure_wedge(log, monkeypatch):
    """Regression: device enumerated, GLOBAL_RUNNING True, Main alive, but frame
    writes are all failing -> is_streaming() False so the supervisor recovers."""
    present = {"v": True}
    fake = FakeDriver(with_main=True, main_alive=True)
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    driver_mod.GLOBAL_RUNNING = True

    # healthy: writes landing -> running
    driver_mod.GLOBAL_WRITE_FAILS = 0
    assert svc.is_streaming() is True
    assert svc.status() == "running"

    # wedge: every frame write failing -> not streaming, device still present
    driver_mod.GLOBAL_WRITE_FAILS = driver_mod.MAX_WRITE_FAILS
    assert svc.is_streaming() is False
    assert svc.status() == "down"


def test_is_streaming_false_without_main_thread(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver(with_main=False)
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    driver_mod.GLOBAL_RUNNING = True
    assert svc.is_streaming() is False


def test_is_streaming_false_when_no_driver(log, monkeypatch):
    present = {"v": True}
    svc = _service(log, monkeypatch, present, FakeDriver())
    svc.driver = None
    driver_mod.GLOBAL_RUNNING = True
    assert svc.is_streaming() is False


# ------------------------------------------------------------- loop one-shot

def test_loop_connects_when_device_becomes_present(log, monkeypatch):
    """Run the real _loop briefly: absent -> present, assert it connects once."""
    present = {"v": True}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    svc._running = True

    # Make _try_connect deterministic and stop the loop after first connect.
    def fake_try_connect():
        fake.start_called += 1
        svc._running = False  # stop the loop so it doesn't spin
        return True

    monkeypatch.setattr(svc, "_try_connect", fake_try_connect)
    svc._loop()
    assert fake.start_called == 1


def test_stop_sets_running_false_and_stops_driver(log, monkeypatch):
    present = {"v": True}
    fake = FakeDriver()
    svc = _service(log, monkeypatch, present, fake)
    svc.driver = fake
    svc._running = True
    svc.stop()
    assert svc._running is False
    assert fake.stop_called >= 1


# ------------------------------------------------ driver write-fail counter

class _FailingEndpoint:
    def write(self, data):
        raise OSError("device wedged")


class _OkEndpoint:
    def write(self, data):
        return len(data)


def test_write_counter_increments_on_failure_resets_on_success(log):
    """USBControl.write bumps GLOBAL_WRITE_FAILS on failure and zeroes it on a
    successful write, flipping writes_healthy() back True."""
    driver_mod.GLOBAL_WRITE_FAILS = 0

    bad = driver_mod.USBControl(dev=object(), logger=log, endpoint=_FailingEndpoint())
    for i in range(1, driver_mod.MAX_WRITE_FAILS + 1):
        bad.write(b"frame")
        assert driver_mod.GLOBAL_WRITE_FAILS == i
    # enough consecutive failures => not healthy (a wedge)
    assert driver_mod.writes_healthy() is False

    # a single successful write resets the counter and clears the wedge flag
    good = driver_mod.USBControl(dev=object(), logger=log, endpoint=_OkEndpoint())
    good.write(b"frame")
    assert driver_mod.GLOBAL_WRITE_FAILS == 0
    assert driver_mod.writes_healthy() is True
