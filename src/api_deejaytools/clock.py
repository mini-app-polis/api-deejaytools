"""The service's one clock: epoch milliseconds, as every timestamp column holds."""

from __future__ import annotations

import time


def now_ms() -> int:
    """The current time in epoch milliseconds."""
    return int(time.time() * 1000)
