"""System + GPU metrics Collector for ttlcd-panel.

Polls CPU/RAM/net via psutil and GPU via pynvml (falling back to nvidia-smi)
on a daemon background thread, caching the latest snapshot. ``snapshot()``
returns the cached dict and never blocks or raises.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

import psutil

try:
    import pynvml  # nvidia-ml-py

    _HAVE_PYNVML = True
except Exception:  # pragma: no cover - import guard
    pynvml = None  # type: ignore[assignment]
    _HAVE_PYNVML = False

_GB = 1024 ** 3


def _empty_snapshot() -> dict:
    """A valid snapshot with zero/None fallbacks (before first poll)."""
    return {
        "ts": 0.0,
        "cpu": {"pct": 0.0, "per_core": [], "freq_mhz": 0.0, "load1": 0.0},
        "ram": {"pct": 0.0, "used_gb": 0.0, "total_gb": 0.0},
        "net": {"up_mbps": 0.0, "down_mbps": 0.0},
        "gpu": None,
    }


class Collector:
    """Background poller of system + GPU metrics.

    Use :meth:`start` to spawn the daemon thread, :meth:`stop` to end it, and
    :meth:`snapshot` to read the latest cached values (never blocks/raises).
    """

    def __init__(self, interval: float = 1.0, logger: logging.Logger | None = None) -> None:
        self.interval = float(interval)
        self.log = logger or logging.getLogger(__name__)

        self._lock = threading.Lock()
        self._cache: dict = _empty_snapshot()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # net delta state
        self._last_net: tuple[int, int] | None = None
        self._last_net_ts: float | None = None

        # GPU backend state
        self._nvml_ok = False
        self._nvml_handle = None
        self._gpu_mode = "none"  # "nvml" | "smi" | "none"
        self._init_gpu()

    # ------------------------------------------------------------------ GPU
    def _init_gpu(self) -> None:
        if _HAVE_PYNVML:
            try:
                pynvml.nvmlInit()
                self._nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                self._nvml_ok = True
                self._gpu_mode = "nvml"
                return
            except Exception as e:  # noqa: BLE001
                self.log.warning("pynvml init failed (%s); trying nvidia-smi", e)
                self._nvml_ok = False
        # probe nvidia-smi
        if self._read_gpu_smi() is not None:
            self._gpu_mode = "smi"
        else:
            self._gpu_mode = "none"
            self.log.info("No GPU available via pynvml or nvidia-smi")

    @staticmethod
    def _decode(b) -> str:
        return b.decode() if isinstance(b, (bytes, bytearray)) else str(b)

    def _read_gpu_nvml(self) -> dict | None:
        h = self._nvml_handle
        try:
            name = self._decode(pynvml.nvmlDeviceGetName(h))
            util = float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            used_gb = mem.used / _GB
            total_gb = mem.total / _GB
            temp = float(pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU))
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0  # mW -> W
            except Exception:  # noqa: BLE001
                power = 0.0
            try:
                fan = float(pynvml.nvmlDeviceGetFanSpeed(h))
            except Exception:  # noqa: BLE001
                fan = 0.0
            mem_pct = (used_gb / total_gb * 100.0) if total_gb > 0 else 0.0
            return {
                "present": True,
                "name": name,
                "util": util,
                "mem_used_gb": used_gb,
                "mem_total_gb": total_gb,
                "mem_pct": mem_pct,
                "temp_c": temp,
                "power_w": power,
                "fan_pct": fan,
            }
        except Exception as e:  # noqa: BLE001
            self.log.warning("pynvml read failed (%s); falling back to nvidia-smi", e)
            self._nvml_ok = False
            self._gpu_mode = "smi"
            return self._read_gpu_smi()

    def _read_gpu_smi(self) -> dict | None:
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
                    "temperature.gpu,power.draw,fan.speed",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if out.returncode != 0 or not out.stdout.strip():
                return None
            line = out.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 7:
                return None
            name = parts[0]

            def _f(s: str) -> float:
                try:
                    return float(s)
                except ValueError:
                    return 0.0

            util = _f(parts[1])
            used_gb = _f(parts[2]) / 1024.0  # MiB -> GiB
            total_gb = _f(parts[3]) / 1024.0
            temp = _f(parts[4])
            power = _f(parts[5])
            fan = _f(parts[6])
            mem_pct = (used_gb / total_gb * 100.0) if total_gb > 0 else 0.0
            return {
                "present": True,
                "name": name,
                "util": util,
                "mem_used_gb": used_gb,
                "mem_total_gb": total_gb,
                "mem_pct": mem_pct,
                "temp_c": temp,
                "power_w": power,
                "fan_pct": fan,
            }
        except Exception:  # noqa: BLE001
            return None

    def _read_gpu(self) -> dict | None:
        if self._gpu_mode == "nvml" and self._nvml_ok:
            return self._read_gpu_nvml()
        if self._gpu_mode == "smi":
            return self._read_gpu_smi()
        return None

    # -------------------------------------------------------------- polling
    def _poll(self) -> dict:
        # CPU (non-blocking; primed at start)
        per_core = psutil.cpu_percent(percpu=True, interval=None)
        pct = sum(per_core) / len(per_core) if per_core else 0.0
        try:
            freq = psutil.cpu_freq()
            freq_mhz = float(freq.current) if freq else 0.0
        except Exception:  # noqa: BLE001
            freq_mhz = 0.0
        try:
            load1 = float(os.getloadavg()[0])
        except (OSError, AttributeError):
            load1 = 0.0

        # RAM
        vm = psutil.virtual_memory()
        ram = {
            "pct": float(vm.percent),
            "used_gb": vm.used / _GB,
            "total_gb": vm.total / _GB,
        }

        # NET (delta -> MB/s)
        now = time.time()
        nio = psutil.net_io_counters()
        up_mbps = down_mbps = 0.0
        if self._last_net is not None and self._last_net_ts is not None:
            dt = now - self._last_net_ts
            if dt > 0:
                up_mbps = (nio.bytes_sent - self._last_net[0]) / dt / (1024 ** 2)
                down_mbps = (nio.bytes_recv - self._last_net[1]) / dt / (1024 ** 2)
                up_mbps = max(0.0, up_mbps)
                down_mbps = max(0.0, down_mbps)
        self._last_net = (nio.bytes_sent, nio.bytes_recv)
        self._last_net_ts = now

        return {
            "ts": now,
            "cpu": {
                "pct": float(pct),
                "per_core": [float(c) for c in per_core],
                "freq_mhz": freq_mhz,
                "load1": load1,
            },
            "ram": ram,
            "net": {"up_mbps": float(up_mbps), "down_mbps": float(down_mbps)},
            "gpu": self._read_gpu(),
        }

    def _run(self) -> None:
        # prime non-blocking cpu_percent so first real read isn't all zeros
        psutil.cpu_percent(percpu=True, interval=None)
        nio = psutil.net_io_counters()
        self._last_net = (nio.bytes_sent, nio.bytes_recv)
        self._last_net_ts = time.time()

        while not self._stop.is_set():
            t0 = time.time()
            try:
                snap = self._poll()
                with self._lock:
                    self._cache = snap
            except Exception as e:  # noqa: BLE001
                self.log.warning("metrics poll failed: %s", e)
            elapsed = time.time() - t0
            self._stop.wait(max(0.0, self.interval - elapsed))

    # ---------------------------------------------------------------- public
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="metrics-collector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.interval + 2.0)
        self._thread = None
        if self._nvml_ok:
            try:
                pynvml.nvmlShutdown()
            except Exception:  # noqa: BLE001
                pass

    def snapshot(self) -> dict:
        """Return the latest cached snapshot. Never blocks or raises."""
        try:
            with self._lock:
                return self._cache
        except Exception:  # noqa: BLE001 - guarantee no-raise
            return _empty_snapshot()
