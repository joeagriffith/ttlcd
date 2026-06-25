"""Multi-run behaviour: coexistence, owner tagging, rotation, and grace/drop.

Covers the new ViewManager contract where multiple runs coexist (keyed by
run_id), each carries an ``owner``, the dashboard rotates between active runs,
and finished runs linger for RUN_GRACE_S before being expired. Also exercises
the new server ``GET /runs`` endpoint and the SDK owner plumbing.

No hardware: a ViewManager over FakeCollector, FastAPI TestClient, and the
stdlib http.server stub from test_client.py.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from ttlcd_panel.client import Panel
from ttlcd_panel.manager import ViewManager, RUN_GRACE_S
from ttlcd_panel.server import create_app

from conftest import FakeCollector


@pytest.fixture
def manager(fake_collector, logger):
    # small rotate window so the rotation maths are easy to drive
    return ViewManager(fake_collector, logger, idle_view="system", rotate_secs=5.0)


# ------------------------------------------------------- coexistence + owner

def test_two_runs_coexist_with_owners(manager):
    rid_a = manager.start_run(project="a", owner="alice")
    rid_b = manager.start_run(project="b", owner="bob")
    assert rid_a != rid_b

    runs = {r["run_id"]: r for r in manager.get_runs()}
    # both stay active — starting b did NOT replace a
    assert set(runs) == {rid_a, rid_b}
    assert runs[rid_a]["owner"] == "alice"
    assert runs[rid_b]["owner"] == "bob"


def test_owner_defaults_to_agent(manager):
    rid = manager.start_run(project="p")
    run = {r["run_id"]: r for r in manager.get_runs()}[rid]
    assert run["owner"] == "agent"


# --------------------------------------------------------------- rotation

def test_select_run_rotates_with_clock(manager, monkeypatch):
    rid_a = manager.start_run(project="a", owner="alice")
    rid_b = manager.start_run(project="b", owner="bob")
    # stable rotation order is by started_at
    order = [r["run_id"] for r in manager._active_runs()]
    total = len(order)
    assert total == 2

    def at(t):
        monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: t)

    # idx = int(t / rotate) % total ; rotate == 5.0
    at(0.0)        # idx 0
    sel = manager.get_run()
    assert sel["run_id"] == order[0]
    assert sel["_rotation"] == [1, total]

    at(5.0)        # idx 1
    sel = manager.get_run()
    assert sel["run_id"] == order[1]
    assert sel["_rotation"] == [2, total]

    at(10.0)       # idx 0 again — full cycle
    sel = manager.get_run()
    assert sel["run_id"] == order[0]
    assert sel["_rotation"] == [1, total]


def test_select_run_none_when_no_active(manager):
    assert manager._select_run() is None
    assert manager.get_run() == {}


# ----------------------------------------------------------------- log_run

def test_log_run_known_id_updates_only_that_run(manager):
    rid_a = manager.start_run(project="a")
    rid_b = manager.start_run(project="b")
    manager.log_run(metrics={"loss": 0.5}, run_id=rid_a)
    runs = {r["run_id"]: r for r in manager.get_runs()}
    assert runs[rid_a]["metrics"] == {"loss": 0.5}
    assert runs[rid_b]["metrics"] == {}   # untouched


def test_log_run_unknown_id_creates_run(manager):
    manager.log_run(metrics={"acc": 0.9}, run_id="custom-id-1")
    runs = {r["run_id"]: r for r in manager.get_runs()}
    assert "custom-id-1" in runs
    assert runs["custom-id-1"]["metrics"]["acc"] == 0.9
    assert runs["custom-id-1"]["status"] == "running"


def test_log_run_no_id_targets_latest_running(manager, monkeypatch):
    base = 1000.0
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    rid_a = manager.start_run(project="a")
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + 1)
    rid_b = manager.start_run(project="b")
    # b is the most-recently-updated running run → log targets it
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + 2)
    manager.log_run(metrics={"loss": 0.1})
    runs = {r["run_id"]: r for r in manager.get_runs()}
    assert runs[rid_b]["metrics"] == {"loss": 0.1}
    assert runs[rid_a]["metrics"] == {}


# --------------------------------------------------------- finish grace/drop

def test_finished_run_lingers_then_dropped(manager, monkeypatch):
    base = 2000.0
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    rid = manager.start_run(project="p")
    manager.finish_run(status="finished", run_id=rid)

    # within grace: still present
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + RUN_GRACE_S - 1)
    manager._expire()
    assert rid in {r["run_id"] for r in manager.get_runs()}

    # past grace: dropped
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + RUN_GRACE_S + 1)
    manager._expire()
    assert manager.get_runs() == []
    assert rid not in manager._runs


def test_finished_run_keeps_other_running(manager, monkeypatch):
    base = 3000.0
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    rid_done = manager.start_run(project="done")
    rid_live = manager.start_run(project="live")
    manager.finish_run(status="finished", run_id=rid_done)
    # past grace: only the finished one is dropped; the running one survives
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + RUN_GRACE_S + 1)
    manager._expire()
    ids = {r["run_id"] for r in manager.get_runs()}
    assert ids == {rid_live}


# ----------------------------------------------------------- server /runs

@pytest.fixture
def client(tmp_path):
    log = logging.getLogger("multirun-srv")
    collector = FakeCollector(gpu=True)
    mgr = ViewManager(collector, log, idle_view="system")
    app = create_app(mgr, collector, lambda: "running", tmp_path / "I.md")
    c = TestClient(app)
    c._manager = mgr  # type: ignore[attr-defined]
    return c


def test_runs_endpoint_shape_and_owner(client):
    assert client.get("/runs").json() == {"runs": []}
    rid = client.post("/run/start", json={"project": "demo", "owner": "carol"}).json()["run_id"]
    body = client.get("/runs").json()
    assert set(body) == {"runs"}
    assert isinstance(body["runs"], list)
    runs = {r["run_id"]: r for r in body["runs"]}
    assert rid in runs
    assert runs[rid]["owner"] == "carol"


def test_runs_endpoint_shows_multiple(client):
    a = client.post("/run/start", json={"project": "a", "owner": "alice"}).json()["run_id"]
    b = client.post("/run/start", json={"project": "b", "owner": "bob"}).json()["run_id"]
    runs = {r["run_id"]: r for r in client.get("/runs").json()["runs"]}
    assert set(runs) == {a, b}
    assert runs[a]["owner"] == "alice"
    assert runs[b]["owner"] == "bob"


# ------------------------------------------------------------- SDK owner

class _Recorder:
    def __init__(self):
        self.calls = []


def _make_handler(recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except ValueError:
                payload = None
            recorder.calls.append((self.path, payload))
            body = {"run_id": "stub-run-123"} if self.path == "/run/start" else {"ok": True}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@pytest.fixture
def stub_server():
    recorder = _Recorder()
    server = HTTPServer(("127.0.0.1", 0), _make_handler(recorder))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}", recorder
    server.shutdown()
    server.server_close()


def test_panel_sends_explicit_owner(stub_server):
    url, rec = stub_server
    Panel(project="p", url=url, owner="dave")
    path, payload = rec.calls[0]
    assert path == "/run/start"
    assert payload["owner"] == "dave"


def test_panel_owner_defaults_to_env(stub_server, monkeypatch):
    url, rec = stub_server
    monkeypatch.setenv("PANEL_OWNER", "env-owner")
    Panel(project="p", url=url)
    _, payload = rec.calls[0]
    assert payload["owner"] == "env-owner"


def test_panel_owner_defaults_to_agent(stub_server, monkeypatch):
    url, rec = stub_server
    monkeypatch.delenv("PANEL_OWNER", raising=False)
    Panel(project="p", url=url)
    _, payload = rec.calls[0]
    assert payload["owner"] == "agent"
