from __future__ import annotations

import io
import json
import logging

import pytest

from agent_runner.metrics import JsonFormatter, emit_metrics


def test_emf_document_shape():
    out = io.StringIO()

    emit_metrics(
        dimensions={"Engine": "native"},
        metrics={"AgentTurns": (1, "Count"), "TurnLatency": (812, "Milliseconds")},
        properties={"session_id": "s1", "turn": 3},
        out=out,
    )

    document = json.loads(out.getvalue())
    spec = document["_aws"]["CloudWatchMetrics"][0]
    assert spec["Namespace"] == "Meditation/Agent"
    assert spec["Dimensions"] == [["Engine"]]
    assert [m["Name"] for m in spec["Metrics"]] == ["AgentTurns", "TurnLatency"]
    assert spec["Metrics"][1]["Unit"] == "Milliseconds"
    assert document["Engine"] == "native"
    assert document["AgentTurns"] == 1 and document["TurnLatency"] == 812
    assert document["session_id"] == "s1" and document["turn"] == 3
    assert isinstance(document["_aws"]["Timestamp"], int)


def test_user_content_cannot_ride_along_as_a_property():
    with pytest.raises(ValueError, match="text"):
        emit_metrics(
            dimensions={"Engine": "native"},
            metrics={"AgentTurns": (1, "Count")},
            properties={"text": "I feel awful"},
            out=io.StringIO(),
        )


def test_json_formatter_includes_allowed_extras_only():
    record = logging.LogRecord(
        "agent_runner.turns", logging.INFO, "f.py", 1, "turn committed", None, None
    )
    record.session_id = "s1"
    record.text = "never"

    line = json.loads(JsonFormatter().format(record))

    assert line == {
        "level": "INFO",
        "logger": "agent_runner.turns",
        "message": "turn committed",
        "session_id": "s1",
    }
