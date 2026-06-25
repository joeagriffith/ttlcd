"""ttlcd_panel — drive a Thermaltake 3.9" bar LCD with system stats, live ML
training dashboards, messages, and a Claude mascot."""
__version__ = "0.1.0"

from .client import Panel  # noqa: E402,F401

__all__ = ["Panel", "__version__"]
