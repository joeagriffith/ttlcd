"""Panel — a thin, resilient HTTP client (SDK) over the ttlcd-panel daemon.

This is the wandb-style interface that ML / agent code uses to drive the
LCD panel. It POSTs to a localhost daemon (default ``127.0.0.1:8770``).

Resilience is the headline guarantee: every network call uses a short
timeout, swallows *all* exceptions, and degrades to a silent no-op after
warning once. Logging to the panel must never raise or stall the caller's
training loop.

Usage
-----
Basic, wandb-style::

    from ttlcd_panel import Panel

    panel = Panel(project="resnet50", epochs=90, steps_per_epoch=1000)
    for epoch in range(90):
        for batch in range(1000):
            loss = train_step()
            panel.log({"loss": loss}, epoch=epoch, batch=batch)
    panel.finish()

As a context manager (auto start + finish, marks ``failed`` on exception)::

    with Panel(project="quick", epochs=3) as panel:
        panel.message("starting up!", level="info")
        panel.log({"acc": 0.99}, step=42)

Module helpers::

    import ttlcd_panel.client as client
    panel = client.init(project="run", epochs=10)   # wandb.init style
    client.message("hello panel")                    # one-off, no run
"""

from __future__ import annotations

import logging
import os
from types import TracebackType
from typing import Any

import requests

__all__ = ["Panel", "init", "message"]

logger = logging.getLogger("ttlcd_panel.client")

#: Short timeout (seconds) for every request, so the panel never stalls
#: the caller's training loop. Must stay <= 0.5s per the contract.
_TIMEOUT = 0.5

#: Default daemon URL (localhost-only by default).
_DEFAULT_URL = "http://127.0.0.1:8770"


def _post(url: str, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST ``payload`` as JSON to ``url + path``.

    Returns the decoded JSON response on success, or ``None`` on any
    failure (network error, timeout, non-2xx, bad JSON). NEVER raises.
    """
    try:
        resp = requests.post(url.rstrip("/") + path, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {}
    except Exception:  # noqa: BLE001 — resilience: swallow EVERYTHING.
        return None


class Panel:
    """A resilient HTTP client to the ttlcd-panel daemon.

    Constructing a :class:`Panel` immediately starts a run on the daemon
    (``POST /run/start``) and stores the returned ``run_id``. If the daemon
    is unreachable the panel warns once (unless ``quiet``) and silently
    becomes a no-op — all subsequent calls return without effect and
    without raising.

    Parameters
    ----------
    project:
        Human-readable run/project name shown on the panel.
    epochs:
        Total number of epochs, if known (drives progress bars).
    steps_per_epoch:
        Number of batches/steps per epoch, if known.
    url:
        Base URL of the daemon. Defaults to ``http://127.0.0.1:8770``.
    quiet:
        If ``True`` (default) suppress the "daemon unreachable" warning.
    config:
        Optional arbitrary run configuration dict (hyperparameters, etc.).

    Examples
    --------
    >>> panel = Panel(project="demo", epochs=5, steps_per_epoch=100)
    >>> panel.log({"loss": 0.5}, epoch=0, batch=0)
    >>> panel.finish()
    """

    def __init__(
        self,
        project: str = "run",
        epochs: int | None = None,
        steps_per_epoch: int | None = None,
        url: str = _DEFAULT_URL,
        quiet: bool = True,
        config: dict[str, Any] | None = None,
        owner: str | None = None,
    ) -> None:
        self.project = project
        self.url = url
        self.quiet = quiet
        #: Identifies which agent owns this run (for the multi-run rotation on the
        #: panel). Defaults to $PANEL_OWNER so an agent can set its identity once.
        self.owner = owner or os.environ.get("PANEL_OWNER") or "agent"
        self.run_id: str | None = None
        #: When True, every call is a silent no-op (daemon unreachable).
        self._dead = False
        #: Ensures the unreachable warning fires at most once.
        self._warned = False

        result = _post(
            self.url,
            "/run/start",
            {
                "project": project,
                "epochs": epochs,
                "steps_per_epoch": steps_per_epoch,
                "config": config,
                "owner": self.owner,
            },
        )
        if result is None:
            self._go_dead()
        else:
            self.run_id = result.get("run_id")

    # -- internals --------------------------------------------------------

    def _go_dead(self) -> None:
        """Enter no-op mode, warning at most once (unless quiet)."""
        self._dead = True
        if not self._warned:
            self._warned = True
            if not self.quiet:
                logger.warning(
                    "ttlcd-panel daemon unreachable at %s — logging disabled "
                    "(this message shown once).",
                    self.url,
                )

    def _send(self, path: str, payload: dict[str, Any]) -> None:
        """POST to ``path`` unless dead; go dead on failure. Never raises."""
        if self._dead:
            return
        if _post(self.url, path, payload) is None:
            self._go_dead()

    # -- public API -------------------------------------------------------

    def log(
        self,
        metrics: dict[str, Any],
        epoch: int | None = None,
        batch: int | None = None,
        step: int | None = None,
    ) -> None:
        """Log a dict of scalar metrics for the current run.

        ``POST /run/log`` with ``{run_id, metrics, epoch, batch, step}``.
        Never raises and never blocks for more than the short timeout.

        Examples
        --------
        >>> panel.log({"loss": 0.31, "acc": 0.92}, epoch=2, batch=17)
        """
        self._send(
            "/run/log",
            {
                "run_id": self.run_id,
                "metrics": metrics,
                "epoch": epoch,
                "batch": batch,
                "step": step,
            },
        )

    def message(self, text: str, duration: float = 5, level: str = "info") -> None:
        """Flash a full-screen message card on the panel.

        ``POST /message`` with ``{text, duration, level}``. ``level`` is one
        of ``"info"``, ``"warn"``, ``"error"``.

        Examples
        --------
        >>> panel.message("checkpoint saved", duration=3, level="info")
        """
        self._send(
            "/message",
            {"text": text, "duration": duration, "level": level},
        )

    def finish(self, status: str = "finished") -> None:
        """Mark the run finished (or ``"failed"``).

        ``POST /run/finish`` with ``{run_id, status}``.

        Examples
        --------
        >>> panel.finish()
        >>> panel.finish(status="failed")
        """
        self._send("/run/finish", {"run_id": self.run_id, "status": status})

    # -- context manager --------------------------------------------------

    def __enter__(self) -> "Panel":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        """Finish the run on exit; ``status="failed"`` if an exception propagated.

        Returns ``False`` so any in-flight exception continues to propagate.
        """
        self.finish(status="failed" if exc_type is not None else "finished")
        return False


def init(**kwargs: Any) -> Panel:
    """Create and return a :class:`Panel` (wandb-style convenience).

    Mirrors the :class:`Panel` constructor.

    Examples
    --------
    >>> import ttlcd_panel.client as client
    >>> panel = client.init(project="run", epochs=10)
    """
    return Panel(**kwargs)


def message(
    text: str,
    duration: float = 5,
    level: str = "info",
    url: str = _DEFAULT_URL,
) -> None:
    """Send a one-off message to the panel without an active run.

    ``POST /message``. Resilient: swallows all errors, never raises.

    Examples
    --------
    >>> import ttlcd_panel.client as client
    >>> client.message("build complete", level="info")
    """
    _post(url, "/message", {"text": text, "duration": duration, "level": level})
