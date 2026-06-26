"""ViewManager — the brain between the API and the panel.

Holds concurrent runs (keyed by run_id, each tagged with an owner), the current
message, and the frame counter. Builds the per-frame ``ctx``, decides the active
view (priority: message > run > idle), and when several runs are active it ROTATES
the training dashboard between them every ``rotate_secs`` seconds. ``render()`` is
the driver's per-frame callback; all mutators are thread-safe and ``render()``
releases the lock before drawing so API calls never block on rendering.
"""
import math
import threading
import time
import uuid
from types import SimpleNamespace

from . import views as views_mod

HISTORY = 120          # values kept per metric for trend rendering
RUN_GRACE_S = 20.0     # keep a finished run on screen this long
STALE_S = 600.0        # evict a run abandoned without finish() (client died) after this long
MAX_METRIC_KEYS = 64   # cap distinct metric names per run (bound memory)
AGENDA_STALE_S = 1800.0   # drop an agenda not refreshed in this long (bounds memory)
MAX_AGENDA_ITEMS = 64     # cap checklist items per agenda (bound memory)
AGENDA_STATUSES = ("done", "doing", "todo")


def _finite(obj):
    """Recursively replace non-finite floats (NaN/Inf) with None so the value can
    always be JSON-encoded — a NaN anywhere in a run dict would 500 /run and /runs."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


class ViewManager:
    def __init__(self, collector, logger, idle_view: str = "system", rotate_secs: float = 5.0):
        self.collector = collector
        self.logger = logger
        self._lock = threading.Lock()
        self._frame = 0
        self._runs = {}            # run_id -> run dict
        self._agendas = {}         # owner -> agenda dict
        self._message = None
        self._rotate = max(1.0, float(rotate_secs))
        self._idle = idle_view if idle_view in ("system", "mascot") else "system"
        self.views = {
            "system": views_mod.SystemView(),
            "training": views_mod.TrainingView(),
            "outcome": views_mod.OutcomeView(),
            "message": views_mod.MessageView(),
            "mascot": views_mod.MascotView(),
            "agenda": views_mod.AgendaView(),
        }

    # ---- driver render callback ------------------------------------------
    def render(self):
        with self._lock:
            self._frame += 1
            self._expire()
            view, name, run, agenda = self._pick_view()
            ctx = SimpleNamespace(
                frame=self._frame,
                t=time.time(),
                metrics=self.collector.snapshot(),
                run=run,
                agenda=agenda,
                message=dict(self._message) if self._message else None,
            )
        try:
            return view.render(ctx)
        except Exception as e:
            self.logger.warning("view '%s' render error: %s", name, e)
            return None

    def _pick_view(self):
        """Resolve ``(view, name, run, agenda)`` for the current frame. Called
        under the lock. ``run`` / ``agenda`` are the (copied) payload for the
        chosen view, or None. Priority: message > rotating displays > idle."""
        if self._message:
            return self.views["message"], "message", None, None
        sel = self._select_display()
        if sel is None:
            name = self._idle
            return self.views.get(name, self.views["system"]), name, None, None
        kind, payload = sel
        if kind == "agenda":
            return self.views["agenda"], "agenda", None, payload
        # A finished/crashed run (lingering in its grace window) shows the
        # celebratory/alarm outcome screen instead of the live dashboard.
        if payload.get("status") in ("finished", "failed"):
            return self.views["outcome"], "outcome", payload, None
        return self.views["training"], "training", payload, None

    def _active_runs(self):
        """Runs eligible for display: running, or finished within the grace window.
        Sorted by start time for a stable rotation order."""
        now = time.time()
        runs = [r for r in self._runs.values()
                if r["status"] == "running" or (now - r["updated_at"]) <= RUN_GRACE_S]
        return sorted(runs, key=lambda r: r["started_at"])

    def _active_agendas(self):
        """Agendas eligible for display: non-empty and refreshed within the stale
        window. Empty agendas are NOT shown — they'd otherwise steal a rotation
        slot to render a useless "no items" card. Sorted by owner for stable order."""
        now = time.time()
        ags = [a for a in self._agendas.values()
               if a["items"] and (now - a["updated_at"]) <= AGENDA_STALE_S]
        return sorted(ags, key=lambda a: a["owner"])

    @staticmethod
    def _snapshot_run(run):
        """Detach a run's mutable ``metrics`` dict / ``history`` lists from the
        live originals, so a view can render them (or FastAPI serialize them)
        OUTSIDE the lock while ``log_run()`` concurrently mutates the originals
        under it — otherwise a concurrent mutation can raise 'dict changed size
        during iteration' on the render/serialize path."""
        m = run.get("metrics")
        if isinstance(m, dict):
            run["metrics"] = dict(m)
        h = run.get("history")
        if isinstance(h, dict):
            run["history"] = {k: list(v) for k, v in h.items()}
        return run

    def _select_display(self):
        """Pick which display (run or agenda) to show this frame, rotating across
        all of them every ``_rotate`` seconds. Returns ``(kind, payload)`` with a
        copied payload, or None when nothing is active. ``_rotation`` is stamped
        per-kind — "run X of N runs" / "agenda X of N agendas" — so a single live
        run never shows a misleading "1/2" just because an agenda is also up."""
        runs = self._active_runs()
        agendas = self._active_agendas()
        n = len(runs) + len(agendas)
        if n == 0:
            return None
        idx = int(time.time() / self._rotate) % n
        if idx < len(runs):
            sel = self._snapshot_run(dict(runs[idx]))
            sel["_rotation"] = [idx + 1, len(runs)]     # 1-based, within runs
            return "run", sel
        aidx = idx - len(runs)
        sel = dict(agendas[aidx])
        sel["_rotation"] = [aidx + 1, len(agendas)]     # 1-based, within agendas
        return "agenda", sel

    def _select_run(self):
        """Pick which run to show, rotating among runs only. Backs ``GET /run``;
        the panel itself rotates across runs *and* agendas via _select_display.
        Its ``_rotation`` matches the on-screen run badge (both count runs only)."""
        active = self._active_runs()
        if not active:
            return None
        idx = int(time.time() / self._rotate) % len(active)
        sel = self._snapshot_run(dict(active[idx]))
        sel["_rotation"] = [idx + 1, len(active)]   # 1-based (current, total) for the view
        return sel

    def _expire(self):
        now = time.time()
        if self._message and self._message["until"] <= now:
            self._message = None
        dead = []
        for rid, r in self._runs.items():
            age = now - r["updated_at"]
            if r["status"] != "running":
                if age > RUN_GRACE_S:
                    dead.append(rid)
            elif age > STALE_S:           # abandoned run: client died without finish()
                dead.append(rid)
        for rid in dead:
            del self._runs[rid]
        stale_ag = [o for o, a in self._agendas.items()
                    if (now - a["updated_at"]) > AGENDA_STALE_S]
        for o in stale_ag:
            del self._agendas[o]

    def _active_name(self):
        # Name the active category via the single selection path (_pick_view), so
        # there's no second copy of the rotation math to keep in sync. The outcome
        # screen is a render-time detail of a finished/failed run, so it still
        # reports as "training" here.
        _view, name, _run, _agenda = self._pick_view()
        return "training" if name == "outcome" else name

    # ---- run helpers ------------------------------------------------------
    def _new_run(self, project="run", epochs=None, steps_per_epoch=None, config=None, owner=None):
        return {
            "run_id": uuid.uuid4().hex[:12],
            "project": project or "run",
            "owner": owner or "agent",
            "status": "running",
            "epochs": int(epochs) if epochs else None,
            "steps_per_epoch": int(steps_per_epoch) if steps_per_epoch else None,
            "epoch": 0, "batch": 0, "global_step": 0,
            "metrics": {}, "history": {},
            "config": _finite(config or {}),
            "started_at": time.time(), "updated_at": time.time(),
        }

    def _resolve_run(self, run_id, owner):
        """Find the run a log/finish call targets, creating one if needed."""
        if run_id is not None:
            r = self._runs.get(run_id)
            if r is None:                       # daemon restarted, or logging before start
                r = self._new_run(owner=owner)
                r["run_id"] = run_id
                self._runs[run_id] = r
            return r
        running = [r for r in self._runs.values() if r["status"] == "running"]
        if running:
            return max(running, key=lambda r: r["updated_at"])
        r = self._new_run(owner=owner)
        self._runs[r["run_id"]] = r
        return r

    # ---- state mutators (called by the server) ---------------------------
    def start_run(self, project="run", epochs=None, steps_per_epoch=None, config=None, owner=None):
        with self._lock:
            r = self._new_run(project, epochs, steps_per_epoch, config, owner)
            self._runs[r["run_id"]] = r
            return r["run_id"]

    def log_run(self, metrics=None, epoch=None, batch=None, step=None, run_id=None, owner=None):
        with self._lock:
            r = self._resolve_run(run_id, owner)
            if r["status"] != "running":
                return                              # late log to a finished/crashed run — ignore
            if owner and r.get("owner") in (None, "agent"):
                r["owner"] = owner
            if epoch is not None:
                r["epoch"] = int(epoch)
            if batch is not None:
                r["batch"] = int(batch)
            if step is not None:
                r["global_step"] = int(step)
            else:
                r["global_step"] += 1
            for k, v in (metrics or {}).items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(fv):       # NaN/Inf would make JSON encoding 500
                    continue
                if k not in r["metrics"] and len(r["metrics"]) >= MAX_METRIC_KEYS:
                    continue                    # bound distinct metric names per run
                r["metrics"][k] = fv
                h = r["history"].setdefault(k, [])
                h.append(fv)
                if len(h) > HISTORY:
                    del h[0:len(h) - HISTORY]
            r["updated_at"] = time.time()

    def finish_run(self, status="finished", run_id=None, owner=None):
        with self._lock:
            r = None
            if run_id is not None:
                r = self._runs.get(run_id)
            else:
                running = [x for x in self._runs.values() if x["status"] == "running"]
                if running:
                    r = max(running, key=lambda x: x["updated_at"])
            if r:
                r["status"] = status if status in ("finished", "failed") else "finished"
                r["updated_at"] = time.time()

    def get_run(self):
        """The run currently on screen (rotation-selected), for back-compat."""
        with self._lock:
            return self._select_run() or {}

    def get_runs(self):
        with self._lock:
            return [self._snapshot_run(dict(r)) for r in self._active_runs()]

    def show_message(self, text, duration=5, level="info"):
        with self._lock:
            self._message = {
                "text": str(text),
                "level": level if level in ("info", "warn", "error") else "info",
                "until": time.time() + max(0.5, float(duration)),
            }

    def set_idle_view(self, name):
        with self._lock:
            if name in ("system", "mascot"):
                self._idle = name
                return True
            return False

    # ---- agenda (agent to-do checklist) ----------------------------------
    def set_agenda(self, owner=None, title=None, items=None):
        """Create or replace an owner's agenda. A new call for the same owner
        fully replaces that owner's checklist. Items are sanitized: each must be
        a ``{"task", "status"}`` dict; unknown statuses coerce to ``"todo"`` and
        the list is capped at ``MAX_AGENDA_ITEMS``. Returns the resolved owner."""
        with self._lock:
            owner = (str(owner).strip() if owner else "") or "agent"
            clean = []
            for it in (items or []):
                if len(clean) >= MAX_AGENDA_ITEMS:       # cap real items, not junk slots
                    break
                if not isinstance(it, dict):             # drop junk BEFORE counting it
                    continue
                status = it.get("status", "todo")
                if status not in AGENDA_STATUSES:
                    status = "todo"
                clean.append({"task": str(it.get("task", "") or ""), "status": status})
            self._agendas[owner] = {
                "owner": owner,
                "title": str(title) if title else "agenda",
                "items": clean,
                "updated_at": time.time(),
            }
            return owner

    def get_agendas(self):
        """All active agendas (deep-copied), in rotation order."""
        with self._lock:
            return [{**a, "items": [dict(it) for it in a["items"]]}
                    for a in self._active_agendas()]
