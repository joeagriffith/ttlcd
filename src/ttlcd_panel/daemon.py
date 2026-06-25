"""paneld — the daemon. Owns the panel, runs the metrics collector, serves the
HTTP API, and keeps the panel alive with a watchdog that re-inits on USB wedges.
"""
import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import uvicorn

from . import __version__, driver as driver_mod
from .driver import LcdDriver
from .manager import ViewManager
from .metrics import Collector
from .server import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770


def _logger(level="INFO"):
    log = logging.getLogger("paneld")
    log.setLevel(getattr(logging, level.upper(), logging.INFO))
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
    return log


class PanelService:
    """Manages the LCD driver lifecycle + a watchdog that recovers from wedges."""

    def __init__(self, manager, logger, image_path, orientation="top"):
        self.manager = manager
        self.logger = logger
        self.image_path = image_path
        self.orientation = orientation
        self.driver = None
        self._running = False
        self._wd = None

    def _build(self):
        return LcdDriver(self.manager.render, self.logger, self.image_path, self.orientation)

    def start(self):
        self._running = True
        self._wd = threading.Thread(target=self._loop, name="panel-supervisor", daemon=True)
        self._wd.start()

    def _try_connect(self):
        """One connect attempt. Returns True iff streaming actually started."""
        try:
            self.driver = self._build()
            self.driver.setup()
            self.driver.start()
            deadline = time.time() + 75
            while time.time() < deadline and self._running:
                if driver_mod.GLOBAL_RUNNING:
                    return True
                time.sleep(0.5)
            self.logger.warning("panel init stalled — will retry")
        except Exception as e:
            self.logger.warning("panel connect failed: %s", e)
        try:
            if self.driver:
                self.driver.stop()
        except Exception:
            pass
        return False

    def is_streaming(self):
        if not self.driver or not driver_mod.GLOBAL_RUNNING:
            return False
        main = next((t for t in self.driver._threads if t.__class__.__name__ == "Main"), None)
        return main is not None and main.is_alive()

    def status(self):
        if self.is_streaming():
            return "running"
        if not LcdDriver.device_present():
            return "disconnected"
        return "down"

    def _loop(self):
        """Supervise the panel: connect when present, recover wedges, wait quietly
        when the device is unplugged, and reconnect the moment it reappears."""
        announced_absent = False
        while self._running:
            if self.is_streaming():
                announced_absent = False
                time.sleep(2.0)
                continue
            if not LcdDriver.device_present():
                if not announced_absent:
                    self.logger.warning("panel not on USB bus — waiting for replug / power-cycle")
                    announced_absent = True
                time.sleep(4.0)
                continue
            announced_absent = False
            # device present but not streaming: (re)connect, clearing any wedge first
            if self.driver:
                try:
                    self.driver.stop()
                    self.driver.reset_usb()
                except Exception:
                    pass
            self.logger.info("panel present — connecting…")
            if self._try_connect():
                self.logger.info("panel streaming ✅")
            else:
                time.sleep(3.0)

    def stop(self):
        self._running = False
        if self.driver:
            try:
                self.driver.stop()
            except Exception:
                pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="paneld", description="Thermaltake LCD panel daemon")
    p.add_argument("--host", default=os.environ.get("PANELD_HOST", DEFAULT_HOST))
    p.add_argument("--port", type=int, default=int(os.environ.get("PANELD_PORT", DEFAULT_PORT)))
    p.add_argument("--idle", default=os.environ.get("PANELD_IDLE", "system"), choices=["system", "mascot"])
    p.add_argument("--orientation", default=os.environ.get("PANELD_ORIENT", "top"))
    p.add_argument("--interval", type=float, default=1.0, help="metrics poll interval (s)")
    p.add_argument("--rotate", type=float, default=float(os.environ.get("PANELD_ROTATE", 5.0)),
                   help="seconds per run when rotating the dashboard across concurrent runs")
    p.add_argument("--log-level", default=os.environ.get("PANELD_LOG", "INFO"))
    p.add_argument("--no-panel", action="store_true", help="run API only, don't touch USB (dev/testing)")
    p.add_argument("--issues", default=str(Path(__file__).resolve().parents[2] / "ISSUES.md"))
    args = p.parse_args(argv)

    log = _logger(args.log_level)
    log.info("paneld %s starting on %s:%d (idle=%s)", __version__, args.host, args.port, args.idle)

    collector = Collector(interval=args.interval, logger=log)
    collector.start()

    manager = ViewManager(collector, log, idle_view=args.idle, rotate_secs=args.rotate)

    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ttlcd-panel"
    cache.mkdir(parents=True, exist_ok=True)
    image_path = str(cache / "frame.jpg")

    service = None
    if not args.no_panel:
        service = PanelService(manager, log, image_path, args.orientation)
        service.start()

    def panel_status():
        return service.status() if service else "disabled"

    app = create_app(manager, collector, panel_status, Path(args.issues))

    def shutdown(*_):
        log.info("shutting down…")
        if service:
            service.stop()
        collector.stop()
        os._exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    main()
