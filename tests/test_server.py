"""FastAPI server endpoint contract tests (no hardware)."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from ttlcd_panel.manager import ViewManager
from ttlcd_panel.server import create_app

from conftest import FakeCollector


@pytest.fixture
def issues_path(tmp_path):
    p = tmp_path / "ISSUES.md"
    p.write_text("## OPEN\n\n## RESOLVED\n_(none yet)_\n")
    return p


@pytest.fixture
def client(issues_path):
    log = logging.getLogger("srv-test")
    collector = FakeCollector(gpu=True)
    manager = ViewManager(collector, log, idle_view="system")
    status = {"v": "running"}
    app = create_app(manager, collector, lambda: status["v"], issues_path)
    c = TestClient(app)
    c._manager = manager  # type: ignore[attr-defined]
    c._collector = collector  # type: ignore[attr-defined]
    c._issues_path = issues_path  # type: ignore[attr-defined]
    return c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert isinstance(j["version"], str)
    assert isinstance(j["uptime_s"], (int, float))
    assert j["panel"] == "running"
    assert j["gpu"] is True


def test_system_returns_snapshot(client):
    r = client.get("/system")
    assert r.status_code == 200
    j = r.json()
    for key in ("ts", "cpu", "ram", "net", "gpu"):
        assert key in j
    assert set(j["cpu"]) == {"pct", "per_core", "freq_mhz", "load1"}


def test_message(client):
    r = client.post("/message", json={"text": "hi", "duration": 3, "level": "warn"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert client._manager._message["text"] == "hi"


def test_run_start_returns_run_id(client):
    r = client.post("/run/start", json={"project": "demo", "epochs": 2, "steps_per_epoch": 5})
    assert r.status_code == 200
    j = r.json()
    assert "run_id" in j and isinstance(j["run_id"], str) and j["run_id"]


def test_run_log_reflected_in_get_run(client):
    rid = client.post("/run/start", json={"project": "demo"}).json()["run_id"]
    r = client.post("/run/log", json={"run_id": rid, "metrics": {"loss": 0.25}, "epoch": 1, "batch": 4})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    run = client.get("/run").json()
    assert run["run_id"] == rid
    assert run["metrics"]["loss"] == 0.25
    assert run["history"]["loss"] == [0.25]
    assert run["epoch"] == 1 and run["batch"] == 4


def test_run_get_empty_when_no_run(client):
    assert client.get("/run").json() == {}


def test_run_finish(client):
    rid = client.post("/run/start", json={"project": "demo"}).json()["run_id"]
    r = client.post("/run/finish", json={"run_id": rid, "status": "failed"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert client.get("/run").json()["status"] == "failed"


def test_view_accept_and_reject(client):
    assert client.post("/view", json={"name": "system"}).json() == {"ok": True}
    assert client.post("/view", json={"name": "mascot"}).json() == {"ok": True}
    assert client.post("/view", json={"name": "bogus"}).json() == {"ok": False}


def test_issue_appended_under_open(client):
    r = client.post(
        "/issue",
        json={"title": "panel glitch", "body": "screen flickered", "agent": "ml-agent", "severity": "high"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    text = client._issues_path.read_text()
    open_idx = text.index("## OPEN")
    resolved_idx = text.index("## RESOLVED")
    block_idx = text.index("panel glitch")
    # the issue block must live between the OPEN and RESOLVED markers
    assert open_idx < block_idx < resolved_idx
    assert "ml-agent" in text
    assert "screen flickered" in text
    assert "high" in text


def test_issue_creates_file_if_missing(tmp_path):
    log = logging.getLogger("srv-test2")
    collector = FakeCollector()
    manager = ViewManager(collector, log)
    missing = tmp_path / "NEW_ISSUES.md"
    app = create_app(manager, collector, lambda: "down", missing)
    c = TestClient(app)
    assert c.post("/issue", json={"title": "t", "body": "b"}).json() == {"ok": True}
    assert missing.exists()
    txt = missing.read_text()
    assert "## OPEN" in txt and "t" in txt


def test_health_gpu_false_when_absent(tmp_path):
    log = logging.getLogger("srv-test3")
    collector = FakeCollector(gpu=False)
    manager = ViewManager(collector, log)
    app = create_app(manager, collector, lambda: "down", tmp_path / "I.md")
    c = TestClient(app)
    j = c.get("/health").json()
    assert j["gpu"] is False
    assert j["panel"] == "down"
