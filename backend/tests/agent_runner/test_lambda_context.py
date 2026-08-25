from __future__ import annotations

import json
import logging
import time

from agent_runner.lambda_context import CONTEXT_HEADER, deadline_from_headers


def test_deadline_from_lambda_context_header():
    deadline_ms = (time.time() + 60) * 1000

    deadline = deadline_from_headers({CONTEXT_HEADER: json.dumps({"deadline": deadline_ms})}, 110)

    # 60 s left, less the 10 s commit margin.
    assert 48 < deadline.remaining() <= 50


def test_fallback_without_header():
    deadline = deadline_from_headers({}, 110)

    assert 109 < deadline.remaining() <= 110


def test_bad_header_falls_back_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        deadline = deadline_from_headers({CONTEXT_HEADER: "not json"}, 30)

    assert 29 < deadline.remaining() <= 30
    assert "unparseable" in caplog.text


def test_past_deadline_is_exhausted_not_negative():
    deadline = deadline_from_headers({CONTEXT_HEADER: '{"deadline": 1}'}, 110)

    assert deadline.remaining() <= 0
    assert deadline.exhausted()
