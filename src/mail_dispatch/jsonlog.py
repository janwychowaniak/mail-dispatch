"""JSON lines on stdout, one event line per request (SPEC §9).

An event carries identifiers, stages, codes and counters — never content, the subject, full
recipient lists, the password or the text of a server reply [D30]. For the same reason an
exception is logged by its type alone: its message can quote what it failed on.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("mail_dispatch")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry["event"] = record.getMessage()
            entry.update(fields)
        else:
            entry["logger"] = record.name
            entry["message"] = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = record.exc_info[0].__name__
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    """Route every logger — uvicorn's included — to one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    LOGGER.log(level, event, extra={"fields": fields})
