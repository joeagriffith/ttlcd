"""FastAPI server exposing the panel over a localhost HTTP API.

`create_app()` wires the routes to a ViewManager + Collector. Kept deliberately
thin: validation + delegation. See ARCHITECTURE.md for the API contract.
"""
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from . import __version__

# Serializes concurrent /issue appends (sync endpoints run in a threadpool).
_issue_lock = threading.Lock()


class MessageBody(BaseModel):
    text: str
    duration: float = 5.0
    level: str = "info"


class RunStartBody(BaseModel):
    project: str = "run"
    epochs: Optional[int] = None
    steps_per_epoch: Optional[int] = None
    config: Optional[dict] = None
    owner: Optional[str] = None


class RunLogBody(BaseModel):
    run_id: Optional[str] = None
    metrics: dict[str, Any] = {}
    epoch: Optional[int] = None
    batch: Optional[int] = None
    step: Optional[int] = None
    owner: Optional[str] = None


class RunFinishBody(BaseModel):
    run_id: Optional[str] = None
    status: str = "finished"
    owner: Optional[str] = None


class ViewBody(BaseModel):
    name: str


class AgendaBody(BaseModel):
    owner: Optional[str] = None
    title: str = "agenda"
    # Untyped items: the manager sanitizes each entry (coerces status, drops
    # non-dicts) — matching the resilient SDK contract — so a stray non-object
    # item drops silently instead of 422-ing the whole agenda.
    items: list[Any] = []


class IssueBody(BaseModel):
    title: str
    body: str = ""
    agent: str = "unknown"
    severity: str = "medium"


def _append_issue(issues_path: Path, issue: IssueBody) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    block = (
        f"### [OPEN] {issue.title}\n"
        f"- **from:** {issue.agent}\n"
        f"- **when:** {stamp}\n"
        f"- **severity:** {issue.severity}\n"
        f"- **what happened:** {issue.body}\n"
        f"- **filed via:** API\n\n"
    )
    with _issue_lock:
        try:
            text = issues_path.read_text() if issues_path.exists() else "## OPEN\n\n## RESOLVED\n"
        except OSError:
            text = "## OPEN\n\n## RESOLVED\n"
        marker = "## OPEN\n"
        if marker in text:
            idx = text.index(marker) + len(marker)
            # drop a "(none yet)" placeholder if present right after the marker
            rest = text[idx:].replace("_(none yet)_\n", "", 1)
            text = text[:idx] + "\n" + block + rest
        else:
            text = marker + "\n" + block + text
        tmp = issues_path.with_name(issues_path.name + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, issues_path)   # atomic: no torn reads / lost updates


def create_app(manager, collector, get_panel_status, issues_path: Path) -> FastAPI:
    app = FastAPI(title="ttlcd-panel", version=__version__)
    started = time.time()

    @app.get("/health")
    def health():
        snap = collector.snapshot()
        return {
            "ok": True,
            "version": __version__,
            "uptime_s": round(time.time() - started, 1),
            "panel": get_panel_status(),
            "gpu": snap.get("gpu") is not None,
        }

    @app.get("/system")
    def system():
        return collector.snapshot()

    @app.post("/message")
    def message(body: MessageBody):
        manager.show_message(body.text, body.duration, body.level)
        return {"ok": True}

    @app.post("/run/start")
    def run_start(body: RunStartBody):
        run_id = manager.start_run(body.project, body.epochs, body.steps_per_epoch,
                                   body.config, body.owner)
        return {"run_id": run_id}

    @app.post("/run/log")
    def run_log(body: RunLogBody):
        manager.log_run(body.metrics, body.epoch, body.batch, body.step, body.run_id, body.owner)
        return {"ok": True}

    @app.post("/run/finish")
    def run_finish(body: RunFinishBody):
        manager.finish_run(body.status, body.run_id, body.owner)
        return {"ok": True}

    @app.get("/run")
    def run_get():
        return manager.get_run()

    @app.get("/runs")
    def runs_get():
        return {"runs": manager.get_runs()}

    @app.post("/view")
    def view(body: ViewBody):
        ok = manager.set_idle_view(body.name)
        return {"ok": ok}

    @app.post("/agenda")
    def agenda_set(body: AgendaBody):
        owner = manager.set_agenda(body.owner, body.title, body.items)
        return {"ok": True, "owner": owner}

    @app.get("/agenda")
    def agenda_get():
        return {"agendas": manager.get_agendas()}

    @app.post("/issue")
    def issue(body: IssueBody):
        _append_issue(Path(issues_path), body)
        return {"ok": True}

    return app
