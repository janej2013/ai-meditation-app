"""The turn's time budget, from the Lambda invocation when there is one.

The Lambda Web Adapter forwards the invocation context as the
``x-amzn-lambda-context`` request header (JSON); its ``deadline`` is the
epoch millisecond at which Lambda will kill the function. The engine
stops asking for tools ten seconds before that so the turn ends with an
answer and a committed checkpoint rather than a timeout. Without the
header -- uvicorn on a laptop -- a fixed budget stands in.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping

from agent.contracts import Deadline

logger = logging.getLogger(__name__)

CONTEXT_HEADER = "x-amzn-lambda-context"
# Kept back for the commit and the final SSE frames.
_COMMIT_MARGIN_SECONDS = 10


def deadline_from_headers(headers: Mapping[str, str], fallback_seconds: float) -> Deadline:
    raw = headers.get(CONTEXT_HEADER)
    if not raw:
        return Deadline.after(fallback_seconds)
    try:
        deadline_ms = float(json.loads(raw)["deadline"])
    except (ValueError, KeyError, TypeError):
        logger.warning("unparseable %s header; using the fallback budget", CONTEXT_HEADER)
        return Deadline.after(fallback_seconds)
    remaining = deadline_ms / 1000 - time.time() - _COMMIT_MARGIN_SECONDS
    return Deadline.after(max(remaining, 0.0))
