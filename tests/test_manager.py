"""ViewManager state machine tests (no hardware)."""
from __future__ import annotations

import time

import pytest
from PIL import Image

from ttlcd_panel.manager import ViewManager, HISTORY, RUN_GRACE_S

from conftest import FakeCollector


@pytest.fixture
def manager(fake_collector, logger):
    return ViewManager(fake_collector, logger, idle_view="system")


def _assert_panel_image(img):
    assert isinstance(img, Image.Image)
    assert img.size == (480, 128)
    assert img.mode == "RGB"


# --------------------------------------------------------------------- idle

def test_idle_defaults_to_system(manager):
    assert manager._active_name() == "system"
    assert manager.get_run() == {}


def test_set_idle_view_accept_and_reject(manager):
    assert manager.set_idle_view("mascot") is True
    assert manager._active_name() == "mascot"
    assert manager.set_idle_view("system") is True
    assert manager._active_name() == "system"
    # rejected: unknown name does not change idle
    assert manager.set_idle_view("training") is False
    assert manager.set_idle_view("bogus") is False
    assert manager._active_name() == "system"


def test_invalid_idle_view_falls_back_to_system(fake_collector, logger):
    m = ViewManager(fake_collector, logger, idle_view="nonsense")
    assert m._active_name() == "system"


# ----------------------------------------------------------------- run flow

def test_start_run_returns_run_id(manager):
    rid = manager.start_run(project="demo", epochs=5, steps_per_epoch=100)
    assert isinstance(rid, str) and rid
    run = manager.get_run()
    assert run["run_id"] == rid
    assert run["project"] == "demo"
    assert run["status"] == "running"
    assert run["epochs"] == 5
    assert run["steps_per_epoch"] == 100
    assert run["metrics"] == {} and run["history"] == {}
    assert manager._active_name() == "training"


def test_log_run_updates_metrics_and_history(manager):
    rid = manager.start_run(project="demo")
    manager.log_run(metrics={"loss": 1.0}, epoch=1, batch=2, step=10, run_id=rid)
    run = manager.get_run()
    assert run["metrics"]["loss"] == 1.0
    assert run["history"]["loss"] == [1.0]
    assert run["epoch"] == 1 and run["batch"] == 2 and run["global_step"] == 10
    # log again, non-float values are skipped
    manager.log_run(metrics={"loss": 0.5, "bad": "x"}, run_id=rid)
    run = manager.get_run()
    assert run["metrics"]["loss"] == 0.5
    assert run["history"]["loss"] == [1.0, 0.5]
    assert "bad" not in run["metrics"]
    # step auto-increments when not provided
    assert run["global_step"] == 11


def test_history_capped_at_120(manager):
    rid = manager.start_run()
    for i in range(HISTORY + 50):
        manager.log_run(metrics={"loss": float(i)}, run_id=rid)
    run = manager.get_run()
    h = run["history"]["loss"]
    assert len(h) == HISTORY
    # the oldest values were dropped; newest retained
    assert h[-1] == float(HISTORY + 50 - 1)
    assert h[0] == float(50)


def test_log_without_start_creates_run(manager):
    manager.log_run(metrics={"acc": 0.9})
    run = manager.get_run()
    assert run and run["status"] == "running"
    assert run["metrics"]["acc"] == 0.9


# ------------------------------------------------------------- message prio

def test_message_takes_priority_over_run(manager):
    manager.start_run()
    assert manager._active_name() == "training"
    manager.show_message("hi", duration=5, level="info")
    assert manager._active_name() == "message"


def test_message_expiry(manager):
    manager.show_message("bye", duration=0.5, level="warn")  # min clamps to 0.5
    assert manager._active_name() == "message"
    time.sleep(0.6)
    manager._expire()
    assert manager._message is None
    assert manager._active_name() == "system"


def test_message_expiry_monkeypatched(manager, monkeypatch):
    base = time.time()
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    manager.show_message("x", duration=5)
    assert manager._active_name() == "message"
    # jump past expiry
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + 6.0)
    manager._expire()
    assert manager._message is None


def test_invalid_level_defaults_to_info(manager):
    manager.show_message("x", level="bogus")
    assert manager._message["level"] == "info"


# ----------------------------------------------------- finish + grace + drop

def test_finish_run_keeps_then_drops(manager, monkeypatch):
    base = time.time()
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    manager.start_run()
    manager.finish_run(status="finished")
    run = manager.get_run()
    assert run["status"] == "finished"
    # within grace, still shows training
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + RUN_GRACE_S - 1)
    manager._expire()
    assert manager._active_name() == "training"
    # past grace, run dropped, back to idle
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + RUN_GRACE_S + 1)
    manager._expire()
    assert manager._runs == {}
    assert manager._active_name() == "system"


def test_finish_invalid_status_coerced(manager):
    # Multi-run: target each run by id and read it back via get_runs(), since
    # get_run() rotates and may select a different (lingering) run.
    rid1 = manager.start_run()
    manager.finish_run(status="weird", run_id=rid1)
    runs = {r["run_id"]: r for r in manager.get_runs()}
    assert runs[rid1]["status"] == "finished"
    rid2 = manager.start_run()
    manager.finish_run(status="failed", run_id=rid2)
    runs = {r["run_id"]: r for r in manager.get_runs()}
    assert runs[rid2]["status"] == "failed"


# --------------------------------------------------------------- rendering

@pytest.mark.parametrize("setup", ["idle_system", "idle_mascot", "run", "message"])
def test_render_returns_panel_image(manager, setup):
    if setup == "idle_mascot":
        manager.set_idle_view("mascot")
    elif setup == "run":
        rid = manager.start_run(project="p", epochs=3, steps_per_epoch=10)
        manager.log_run(metrics={"loss": 0.5, "acc": 0.9}, epoch=1, batch=3, run_id=rid)
    elif setup == "message":
        manager.show_message("hello", duration=5, level="error")
    img = manager.render()
    _assert_panel_image(img)


def test_render_increments_frame(manager):
    f0 = manager._frame
    manager.render()
    manager.render()
    assert manager._frame == f0 + 2


def test_render_never_raises_on_view_error(manager, monkeypatch):
    class Boom:
        def render(self, ctx):
            raise RuntimeError("kaboom")

    manager.views["system"] = Boom()
    # render should swallow the exception and return None, not raise
    assert manager.render() is None


def test_render_no_gpu_does_not_crash(logger):
    m = ViewManager(FakeCollector(gpu=False), logger, idle_view="system")
    _assert_panel_image(m.render())
