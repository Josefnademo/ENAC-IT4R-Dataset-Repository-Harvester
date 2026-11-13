"""Thread-safe ring-buffer logger that streams to stdout and to the dashboard.

Both scrapers run inside worker threads, so every log write is guarded by a
lock. The browser polls ``GET /api/logs?since=<cursor>`` and receives only the
entries it has not seen yet.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List

from .config import APP_NAME, LOG_BUFFER_SIZE

LOGGER_NAME = "enac_it4r.harvester"
FORMAT = "[%(levelname)s] %(message)s"

_lock = threading.Lock()
_cursor = 0
_buffer: Deque[Dict[str, Any]] = deque(maxlen=LOG_BUFFER_SIZE)

_configured = False


class RingBufferHandler(logging.Handler):
    """Keep the last N formatted records in memory for the live console."""

    def emit(self, record: logging.LogRecord) -> None:
        global _cursor
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        entry = {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "message": message,
        }
        with _lock:
            _cursor += 1
            entry["id"] = _cursor
            _buffer.append(entry)


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach the stdout and ring-buffer handlers exactly once."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if _configured:
        return logger

    # Windows consoles default to an ANSI code page; never crash on accents.
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    formatter = logging.Formatter(FORMAT)

    console = logging.StreamHandler(stream)
    console.setFormatter(formatter)
    logger.addHandler(console)

    ring = RingBufferHandler()
    ring.setFormatter(formatter)
    logger.addHandler(ring)

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    """Return the shared application logger (configured on first use)."""
    return configure_logging()


def log_snapshot(since: int = 0) -> Dict[str, Any]:
    """Return every buffered entry newer than ``since`` plus the new cursor."""
    with _lock:
        entries: List[Dict[str, Any]] = [e for e in _buffer if e.get("id", 0) > since]
        cursor = _cursor
    return {"cursor": cursor, "entries": entries, "app": APP_NAME}


def reset_logs() -> None:
    """Clear the ring buffer (used by tests and by ``--harvest`` CLI runs)."""
    global _cursor
    with _lock:
        _buffer.clear()
        _cursor = 0
