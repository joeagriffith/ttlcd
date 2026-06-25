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


class ViewManager:
    def __init__(self, collector, logger, idle_view: str = "system", rotate_secs: float = 5.0):
        self.collector = collector
        self.logger = logger
        self._lock = threading.Lock()
        self._frame = 0
        self._runs = {}            # run_id -> run dict
        self._message = None
        self._rotate = max(1.0, float(rotate_secs))
        self._idle = idle_view if idle_view in ("system", "mascot") else "system"
        self.views = {
            "system": views_mod.SystemView(),
            "training": views_mod.TrainingView(),
            "message": views_mod.MessageView(),
            "mascot": views_mod.MascotView(),
        }

    # ---- driver render callback ------------------------------------------
    def render(self):
        with self._lock:
            self._frame += 1
            self._expire()
            name = self._active_name()
            view = self.views.get(name, self.views["system"])
            run = self._select_run() if name == "training" else None
            ctx = SimpleNamespace(
                frame=self._frame,
                t=time.time(),
                metrics=self.collector.snapshot(),
                run=run,
                message=dict(self._message) if self._message else None,
            )
        try:
            return view.render(ctx)
        except Exception as e:
            self.logger.warning("view '%s' render error: %s", name, e)
            return None

    def _active_runs(self):
        """Runs eligible for display: running, or finished within the grace window.
        Sorted by start time for a stable rotation order."""
        now = time.time()
        runs = [r for r in self._runs.values()
                if r["status"] == "running" or (now - r["updated_at"]) <= RUN_GRACE_S]
        return sorted(runs, key=lambda r: r["started_at"])

    def _select_run(self):
        """Pick which run to show this frame, rotating every ``_rotate`` seconds."""
        active = self._active_runs()
        if not active:
            return None
        idx = int(time.time() / self._rotate) % len(active)
        sel = dict(active[idx])
        sel["_rotation"] = [idx + 1, len(active)]   # 1-based (current, total) for the view
        return sel

    def _expire(self):
        now = time.time()
        if self._message and self._message["until"] <= now:
            self._message = None
        dead = [rid for rid, r in self._runs.items()
                if r["status"] != "running" and (now - r["updated_at"]) > RUN_GRACE_S]
        for rid in dead:
            del self._runs[rid]

    def _active_name(self):
        if self._message:
            return "message"
        if self._active_runs():
            return "training"
        return self._idle

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
            "config": config or {},
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
                r["metrics"][k] = fv
                h = r["history"].setdefault(k, [])
                h.append(fv)
                if len(h) > HISTORY:
                    del h[0:len(h) - HISTORY]
            r["status"] = "running"
            r["updated_at"] = time.time()

    def finish_run(self, status="finished", run_id=None, owner=None):
        with self._lock:
            r = None
            if run_id is not None:
                r = self._runs.get(run_id)
            else:
                running = [x for x in self._runs.values() if x["status"] == "running"]
                if running:
                    r = max(running, key=lambda x: x["started_at"])
            if r:
                r["status"] = status if status in ("finished", "failed") else "finished"
                r["updated_at"] = time.time()

    def get_run(self):
        """The run currently on screen (rotation-selected), for back-compat."""
        with self._lock:
            return self._select_run() or {}

    def get_runs(self):
        with self._lock:
            return [dict(r) for r in self._active_runs()]

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
