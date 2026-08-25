"""Observability the harness owns: CloudWatch metrics via EMF, and JSON logs.

Embedded Metric Format is a JSON line on stdout that CloudWatch Logs turns
into metrics -- no client, no extra IAM, no dependency. Written by hand
because the document is small and the allowed properties are a policy:
ids, counts and reasons only, never a word of user content (constraint 7).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

NAMESPACE = "Meditation/Agent"

# Everything else is refused: properties are searchable in Logs Insights,
# and user text must never be.
ALLOWED_PROPERTIES = frozenset({"session_id", "turn", "reason", "model_id"})


def emit_metrics(
    *,
    dimensions: Mapping[str, str],
    metrics: Mapping[str, tuple[float, str]],
    properties: Mapping[str, Any] | None = None,
    namespace: str = NAMESPACE,
    out: TextIO | None = None,
) -> None:
    """One EMF document. ``metrics`` maps name -> (value, unit)."""
    properties = dict(properties or {})
    forbidden = set(properties) - ALLOWED_PROPERTIES
    if forbidden:
        raise ValueError(f"metric properties not allowed: {sorted(forbidden)}")
    document: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [
                        {"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()
                    ],
                }
            ],
        },
        **dimensions,
        **{name: value for name, (value, _) in metrics.items()},
        **properties,
    }
    stream = out or sys.stdout
    stream.write(json.dumps(document, separators=(",", ":")) + "\n")
    stream.flush()


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, so Logs Insights can filter on fields.

    Only the message and the standard fields; anything a caller wants
    searchable goes through ``extra`` with one of the allowed names.
    """

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ALLOWED_PROPERTIES:
            value = getattr(record, key, None)
            if value is not None:
                line[key] = value
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
