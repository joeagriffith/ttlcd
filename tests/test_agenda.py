"""Agent agenda / to-do checklist: manager state, view rendering, the
``/agenda`` endpoints, and the SDK plumbing. No hardware.

The agenda is an owner-keyed checklist an agent publishes so a human can glance
at the panel and see what's done / doing / queued. It coexists with run
dashboards in the manager's rotation (see ARCHITECTURE.md "View selection").
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import ttlcd_panel.client as client_mod
from ttlcd_panel.client import Panel
from ttlcd_panel.manager import (
    ViewManager, AGENDA_STALE_S, MAX_AGENDA_ITEMS,
)
from ttlcd_panel.server import create_app
from ttlcd_panel.views import AgendaView

from conftest import FakeCollector


@pytest.fixture
def manager(fake_collector, logger):
    return ViewManager(fake_collector, logger, idle_view="system", rotate_secs=5.0)


def _assert_panel_image(img):
    assert isinstance(img, Image.Image)
    assert img.size == (480, 128)
    assert img.mode == "RGB"


# ----------------------------------------------------------- manager: state

def test_set_agenda_stores_and_reads_back(manager):
    manager.set_agenda(owner="alice", title="nightly",
                       items=[{"task": "load", "status": "done"},
                              {"task": "train", "status": "doing"}])
    ags = manager.get_agendas()
    assert len(ags) == 1
    a = ags[0]
    assert a["owner"] == "alice"
    assert a["title"] == "nightly"
    assert [it["status"] for it in a["items"]] == ["done", "doing"]
    assert [it["task"] for it in a["items"]] == ["load", "train"]


def test_set_agenda_replaces_same_owner(manager):
    manager.set_agenda(owner="bob", items=[{"task": "a", "status": "todo"}])
    manager.set_agenda(owner="bob", items=[{"task": "b", "status": "done"}])
    ags = manager.get_agendas()
    assert len(ags) == 1                       # replaced, not appended
    assert [it["task"] for it in ags[0]["items"]] == ["b"]


def test_agendas_keyed_per_owner(manager):
    manager.set_agenda(owner="alice", items=[{"task": "x", "status": "todo"}])
    manager.set_agenda(owner="bob", items=[{"task": "y", "status": "todo"}])
    owners = {a["owner"] for a in manager.get_agendas()}
    assert owners == {"alice", "bob"}


def test_set_agenda_defaults_owner_and_title(manager):
    manager.set_agenda(items=[{"task": "x", "status": "todo"}])
    a = manager.get_agendas()[0]
    assert a["owner"] == "agent"
    assert a["title"] == "agenda"


def test_set_agenda_sanitizes_status_and_task(manager):
    manager.set_agenda(owner="a", items=[
        {"task": "ok", "status": "bogus"},      # unknown -> todo
        {"task": 123, "status": "done"},        # non-str task -> str
        "not a dict",                            # dropped
        {"status": "doing"},                     # missing task -> ""
    ])
    items = manager.get_agendas()[0]["items"]
    assert items[0] == {"task": "ok", "status": "todo"}
    assert items[1] == {"task": "123", "status": "done"}
    assert items[2] == {"task": "", "status": "doing"}
    assert len(items) == 3


def test_set_agenda_caps_items(manager):
    manager.set_agenda(owner="a", items=[{"task": str(i), "status": "todo"}
                                         for i in range(MAX_AGENDA_ITEMS + 20)])
    assert len(manager.get_agendas()[0]["items"]) == MAX_AGENDA_ITEMS


def test_set_agenda_junk_does_not_consume_cap(manager):
    # The cap counts real (dict) items, not junk: junk entries interleaved with
    # MAX valid items must NOT push valid items out of the cap.
    raw = []
    for i in range(MAX_AGENDA_ITEMS):
        raw.append("junk")                       # non-dict — must not count
        raw.append({"task": str(i), "status": "todo"})
    manager.set_agenda(owner="a", items=raw)
    items = manager.get_agendas()[0]["items"]
    assert len(items) == MAX_AGENDA_ITEMS
    assert items[-1]["task"] == str(MAX_AGENDA_ITEMS - 1)   # last valid survived


def test_get_runs_detaches_metrics_and_history(manager):
    # Exported run copies must not share the live metrics dict / history lists,
    # so a consumer iterating them outside the lock can't be corrupted by a
    # concurrent log_run() (and can't mutate live state).
    rid = manager.start_run(project="p")
    manager.log_run(metrics={"loss": 1.0}, run_id=rid)
    runs = manager.get_runs()
    runs[0]["metrics"]["loss"] = 999
    runs[0]["history"]["loss"].append(999)
    fresh = manager.get_runs()[0]
    assert fresh["metrics"]["loss"] == 1.0
    assert fresh["history"]["loss"] == [1.0]


def test_get_agendas_is_a_copy(manager):
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "todo"}])
    ags = manager.get_agendas()
    ags[0]["items"][0]["status"] = "done"       # mutate the copy
    assert manager.get_agendas()[0]["items"][0]["status"] == "todo"


# ----------------------------------------------------------- manager: expiry

def test_agenda_expires_when_stale(manager, monkeypatch):
    base = 1000.0
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base)
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "todo"}])
    # within stale window: still present
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + AGENDA_STALE_S - 1)
    manager._expire()
    assert manager.get_agendas()
    # past stale window: dropped
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: base + AGENDA_STALE_S + 1)
    manager._expire()
    assert manager.get_agendas() == []
    assert manager._agendas == {}


# ------------------------------------------------------ manager: selection

def test_agenda_shows_when_no_runs(manager):
    assert manager._active_name() == "system"        # idle
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "todo"}])
    assert manager._active_name() == "agenda"


def test_empty_agenda_is_not_displayed(manager):
    # An items-less agenda must not steal a rotation slot (it'd render a useless
    # "no items" card). It stays idle / shows the run instead, and is hidden from
    # the active list.
    manager.set_agenda(owner="a", title="empty", items=[])
    assert manager._active_name() == "system"
    assert manager.get_agendas() == []
    # ...and it doesn't dilute a live run either
    manager.start_run(project="p", owner="r")
    assert manager._active_name() == "training"


def test_message_takes_priority_over_agenda(manager):
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "todo"}])
    assert manager._active_name() == "agenda"
    manager.show_message("hi", duration=5)
    assert manager._active_name() == "message"


def test_agenda_and_run_rotate_together(manager, monkeypatch):
    manager.start_run(project="p", owner="runner")
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "todo"}])

    def at(t):
        monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: t)

    # The slot rotates run <-> agenda, but each badge counts WITHIN its own kind,
    # so a single run shows [1,1] (no misleading "1/2") even though an agenda
    # shares the rotation.
    at(0.0)        # idx 0 -> the run
    assert manager._active_name() == "training"
    sel = manager._select_display()
    assert sel[0] == "run" and sel[1]["_rotation"] == [1, 1]

    at(5.0)        # idx 1 -> the agenda
    assert manager._active_name() == "agenda"
    sel = manager._select_display()
    assert sel[0] == "agenda" and sel[1]["_rotation"] == [1, 1]


def test_rotation_badge_counts_within_kind(manager, monkeypatch):
    # 2 runs + 1 agenda: a run badge reads "X of 2 runs"; the agenda "1 of 1".
    manager.start_run(project="r1", owner="a")
    manager.start_run(project="r2", owner="b")
    manager.set_agenda(owner="c", items=[{"task": "x", "status": "todo"}])

    def at(t):
        monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: t)

    at(0.0)        # idx 0 -> first run
    k, sel = manager._select_display()
    assert k == "run" and sel["_rotation"] == [1, 2]
    at(5.0)        # idx 1 -> second run
    k, sel = manager._select_display()
    assert k == "run" and sel["_rotation"] == [2, 2]
    at(10.0)       # idx 2 -> the agenda, counted alone
    k, sel = manager._select_display()
    assert k == "agenda" and sel["_rotation"] == [1, 1]


def test_get_run_rotation_matches_run_badge(manager, monkeypatch):
    # GET /run rotates among runs only; its _rotation total must match the
    # on-screen run badge (both count runs), even with an agenda present.
    manager.start_run(project="r1", owner="a")
    manager.start_run(project="r2", owner="b")
    manager.set_agenda(owner="c", items=[{"task": "x", "status": "todo"}])
    monkeypatch.setattr("ttlcd_panel.manager.time.time", lambda: 0.0)
    assert manager.get_run()["_rotation"] == [1, 2]


def test_render_routes_to_agenda_view(manager):
    class Spy:
        def __init__(self):
            self.calls = 0

        def render(self, ctx):
            self.calls += 1
            assert ctx.agenda is not None and ctx.run is None
            return Image.new("RGB", (480, 128), (0, 0, 0))

    spy = Spy()
    manager.views["agenda"] = spy
    manager.set_agenda(owner="a", items=[{"task": "x", "status": "doing"}])
    manager.render()
    assert spy.calls == 1


# ---------------------------------------------------------------- the view

def _ctx(agenda, frame=3):
    return SimpleNamespace(frame=frame, t=0.0, metrics={}, run=None,
                           agenda=agenda, message=None)


def test_agendaview_renders_mixed_statuses():
    v = AgendaView()
    ag = {"owner": "alice", "title": "nightly", "items": [
        {"task": "load data", "status": "done"},
        {"task": "train model", "status": "doing"},
        {"task": "evaluate", "status": "todo"},
    ]}
    _assert_panel_image(v.render(_ctx(ag)))


def test_agendaview_renders_empty_items():
    v = AgendaView()
    _assert_panel_image(v.render(_ctx({"owner": "a", "title": "t", "items": []})))


def test_agendaview_renders_none_agenda():
    v = AgendaView()
    _assert_panel_image(v.render(_ctx(None)))


def test_agendaview_scrolls_long_list_over_frames():
    v = AgendaView()
    items = [{"task": "task %d" % i, "status": "todo"} for i in range(20)]
    ag = {"owner": "a", "title": "big", "items": items}
    # render at a spread of frames — the auto-scroll window must never crash
    for frame in (0, 30, 60, 200, 1000):
        _assert_panel_image(v.render(_ctx(ag, frame=frame)))


def test_agendaview_scroll_per_owner_advances():
    # Scroll advances per on-screen render call, NOT the global frame counter (so
    # rotation off/on doesn't make the window jump), and is tracked PER owner so
    # alternating agendas don't reset each other.
    v = AgendaView()
    items = [{"task": "task %d" % i, "status": "todo"} for i in range(10)]
    a = {"owner": "a", "title": "t", "items": items}
    b = {"owner": "b", "title": "t", "items": items}
    for _ in range(5):
        v.render(_ctx(a, frame=0))                  # global frame pinned at 0
    assert v._scroll["a"] == 5                       # advanced anyway
    v.render(_ctx(b, frame=0))                       # a different owner takes the slot
    assert v._scroll["a"] == 5 and v._scroll["b"] == 1   # a's progress preserved
    v.render(_ctx(a, frame=0))                       # back to a — continues, not reset
    assert v._scroll["a"] == 6


def test_agendaview_handles_rotation_badge_and_bad_items():
    v = AgendaView()
    ag = {"owner": "a" * 40, "title": "x" * 80, "_rotation": [2, 3], "items": [
        {"task": "y" * 200, "status": "weird"},     # over-long + unknown status
    ]}
    _assert_panel_image(v.render(_ctx(ag)))


# ---------------------------------------------------------------- server API

@pytest.fixture
def client(tmp_path):
    log = logging.getLogger("agenda-srv")
    collector = FakeCollector(gpu=True)
    mgr = ViewManager(collector, log, idle_view="system")
    app = create_app(mgr, collector, lambda: "running", tmp_path / "I.md")
    c = TestClient(app)
    c._manager = mgr  # type: ignore[attr-defined]
    return c


def test_agenda_get_empty(client):
    assert client.get("/agenda").json() == {"agendas": []}


def test_agenda_post_then_get(client):
    r = client.post("/agenda", json={
        "owner": "carol", "title": "deploy",
        "items": [{"task": "build", "status": "done"},
                  {"task": "ship", "status": "doing"}],
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "owner": "carol"}

    body = client.get("/agenda").json()
    assert set(body) == {"agendas"}
    ags = {a["owner"]: a for a in body["agendas"]}
    assert "carol" in ags
    assert ags["carol"]["title"] == "deploy"
    assert [it["status"] for it in ags["carol"]["items"]] == ["done", "doing"]


def test_agenda_post_defaults_owner(client):
    r = client.post("/agenda", json={"items": [{"task": "x", "status": "todo"}]})
    assert r.json() == {"ok": True, "owner": "agent"}


def test_agenda_post_replaces_same_owner(client):
    client.post("/agenda", json={"owner": "d", "items": [{"task": "a", "status": "todo"}]})
    client.post("/agenda", json={"owner": "d", "items": [{"task": "b", "status": "done"}]})
    ags = client.get("/agenda").json()["agendas"]
    assert len(ags) == 1
    assert [it["task"] for it in ags[0]["items"]] == ["b"]


def test_agenda_post_non_dict_items_dropped_not_422(client):
    # Resilient contract: a stray non-object item is dropped, not 422'd — the
    # rest of the agenda is accepted (mirrors the SDK/manager sanitization).
    r = client.post("/agenda", json={
        "owner": "e",
        "items": ["build", {"task": "ship", "status": "doing"}, 42],
    })
    assert r.status_code == 200
    items = {a["owner"]: a for a in client.get("/agenda").json()["agendas"]}["e"]["items"]
    assert items == [{"task": "ship", "status": "doing"}]


# ------------------------------------------------------------------ SDK

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


def test_panel_agenda_payload(stub_server):
    url, rec = stub_server
    panel = Panel(project="p", url=url, owner="dave")
    rec.calls.clear()
    panel.agenda([{"task": "compile", "status": "doing"}], title="ci")
    path, payload = rec.calls[0]
    assert path == "/agenda"
    assert payload == {
        "owner": "dave",
        "title": "ci",
        "items": [{"task": "compile", "status": "doing"}],
    }


def test_module_agenda_helper(stub_server):
    url, rec = stub_server
    client_mod.agenda("builder", [{"task": "x", "status": "todo"}], url=url)
    path, payload = rec.calls[0]
    assert path == "/agenda"
    assert payload == {
        "owner": "builder",
        "title": "agenda",
        "items": [{"task": "x", "status": "todo"}],
    }


def test_panel_agenda_dead_url_never_raises():
    panel = Panel(project="x", url="http://127.0.0.1:1", quiet=True)
    panel.agenda([{"task": "x", "status": "todo"}])   # must be a silent no-op


def test_panel_agenda_failure_does_not_disable_logging(stub_server, monkeypatch):
    # A failed agenda POST must NOT latch the Panel into no-op mode — the run's
    # own log()/finish() must keep working afterward.
    url, rec = stub_server
    panel = Panel(project="p", url=url)
    assert panel._dead is False
    # make just the agenda POST fail
    import ttlcd_panel.client as cm
    real_post = cm._post

    def flaky_post(u, path, payload):
        if path == "/agenda":
            return None                     # simulate timeout / non-2xx
        return real_post(u, path, payload)

    monkeypatch.setattr(cm, "_post", flaky_post)
    panel.agenda([{"task": "x", "status": "todo"}])
    assert panel._dead is False             # not disabled by the agenda failure
    rec.calls.clear()
    panel.log({"loss": 0.1})                # logging still works
    assert rec.calls and rec.calls[0][0] == "/run/log"


def test_cli_agenda_preserves_colon_in_task(stub_server):
    # A task containing a colon (e.g. a URL) must not be split as 'status:task'
    # unless the prefix is an actual status.
    from ttlcd_panel import cli
    url, rec = stub_server
    rc = cli.main(["--url", url, "agenda", "--owner", "z",
                   "--item", "https://h: rebuild", "--item", "done:finish"])
    assert rc == 0
    path, payload = rec.calls[0]
    assert path == "/agenda"
    assert payload["items"] == [
        {"task": "https://h: rebuild", "status": "todo"},
        {"task": "finish", "status": "done"},
    ]
