"""Panel SDK (HTTP client) resilience + payload contract tests.

Uses a tiny stdlib http.server in a background thread as the daemon stub —
no FastAPI, no hardware.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import ttlcd_panel.client as client_mod
from ttlcd_panel.client import Panel


# --------------------------------------------------------- stub HTTP server

class _Recorder:
    def __init__(self):
        self.calls = []  # list of (path, payload dict)


def _make_handler(recorder: _Recorder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode() or "{}")
            except ValueError:
                payload = None
            recorder.calls.append((self.path, payload))
            body = {"ok": True}
            if self.path == "/run/start":
                body = {"run_id": "stub-run-123"}
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
    url = f"http://{host}:{port}"
    yield url, recorder
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------- resilience

def test_dead_url_never_raises_and_no_run_id():
    # Port 1 is not listening; constructing must not raise.
    panel = Panel(project="x", url="http://127.0.0.1:1", quiet=True)
    assert panel._dead is True
    assert panel.run_id is None
    # all subsequent calls are silent no-ops
    panel.log({"loss": 1.0}, epoch=0)
    panel.message("hi")
    panel.finish()


def test_dead_url_warns_at_most_once(caplog):
    with caplog.at_level(logging.WARNING, logger="ttlcd_panel.client"):
        panel = Panel(project="x", url="http://127.0.0.1:1", quiet=False)
        panel.log({"a": 1})
        panel.message("b")
        panel.finish()
    warnings = [r for r in caplog.records if "unreachable" in r.getMessage()]
    assert len(warnings) == 1


def test_quiet_dead_url_emits_no_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="ttlcd_panel.client"):
        Panel(project="x", url="http://127.0.0.1:1", quiet=True)
    assert not [r for r in caplog.records if "unreachable" in r.getMessage()]


# --------------------------------------------------------- payload contracts

def test_start_payload(stub_server):
    url, rec = stub_server
    panel = Panel(project="resnet", epochs=90, steps_per_epoch=1000, url=url, config={"lr": 0.1})
    assert panel.run_id == "stub-run-123"
    assert panel._dead is False
    path, payload = rec.calls[0]
    assert path == "/run/start"
    assert payload == {
        "project": "resnet",
        "epochs": 90,
        "steps_per_epoch": 1000,
        "config": {"lr": 0.1},
        "owner": "agent",
    }


def test_log_payload(stub_server):
    url, rec = stub_server
    panel = Panel(project="p", url=url)
    rec.calls.clear()
    panel.log({"loss": 0.31, "acc": 0.92}, epoch=2, batch=17, step=42)
    path, payload = rec.calls[0]
    assert path == "/run/log"
    assert payload == {
        "run_id": "stub-run-123",
        "metrics": {"loss": 0.31, "acc": 0.92},
        "epoch": 2,
        "batch": 17,
        "step": 42,
    }


def test_message_payload(stub_server):
    url, rec = stub_server
    panel = Panel(project="p", url=url)
    rec.calls.clear()
    panel.message("checkpoint saved", duration=3, level="warn")
    path, payload = rec.calls[0]
    assert path == "/message"
    assert payload == {"text": "checkpoint saved", "duration": 3, "level": "warn"}


def test_finish_payload(stub_server):
    url, rec = stub_server
    panel = Panel(project="p", url=url)
    rec.calls.clear()
    panel.finish(status="failed")
    path, payload = rec.calls[0]
    assert path == "/run/finish"
    assert payload == {"run_id": "stub-run-123", "status": "failed"}


def test_module_message_helper(stub_server):
    url, rec = stub_server
    client_mod.message("build complete", level="info", url=url)
    path, payload = rec.calls[0]
    assert path == "/message"
    assert payload == {"text": "build complete", "duration": 5, "level": "info"}


def test_init_helper(stub_server):
    url, _ = stub_server
    panel = client_mod.init(project="run", epochs=10, url=url)
    assert isinstance(panel, Panel)
    assert panel.run_id == "stub-run-123"


# ------------------------------------------------------------ context manager

def test_context_manager_finishes_clean(stub_server):
    url, rec = stub_server
    with Panel(project="p", url=url) as panel:
        rec.calls.clear()
        panel.log({"x": 1})
    # last call should be a finish with status=finished
    finish_calls = [c for c in rec.calls if c[0] == "/run/finish"]
    assert finish_calls
    assert finish_calls[-1][1]["status"] == "finished"


def test_context_manager_marks_failed_on_exception(stub_server):
    url, rec = stub_server
    with pytest.raises(ValueError):
        with Panel(project="p", url=url) as panel:
            rec.calls.clear()
            raise ValueError("boom")
    finish_calls = [c for c in rec.calls if c[0] == "/run/finish"]
    assert finish_calls
    assert finish_calls[-1][1]["status"] == "failed"
