"""Static checks on the system prompt: the crisis words are there, the
tools are named, nothing that would break the cache prefix slipped in."""

from __future__ import annotations

import re

from agent.budget import FINALIZE_TOOL_NAME
from agent.prompt import CRISIS_TEXT, REFUSAL_TEXT, SYSTEM_PROMPT, render_memory_block


def test_prompt_is_filled_in():
    assert len(SYSTEM_PROMPT) > 1000


def test_prompt_carries_the_crisis_text_verbatim():
    assert CRISIS_TEXT in SYSTEM_PROMPT
    for phrase in ("13 11 14", "1300 22 4636", "000", "Beyond Blue", "Lifeline"):
        assert phrase in CRISIS_TEXT


def test_crisis_and_refusal_texts_differ():
    assert CRISIS_TEXT != REFUSAL_TEXT


def test_prompt_names_every_tool():
    for name in (FINALIZE_TOOL_NAME, "save_user_insight", "get_session_history"):
        assert name in SYSTEM_PROMPT


def test_prompt_has_no_template_residue_and_no_dates():
    assert "{" not in SYSTEM_PROMPT and "}" not in SYSTEM_PROMPT
    assert not re.search(r"\b20\d\d\b", SYSTEM_PROMPT)


def test_prompt_states_the_boundaries():
    for phrase in ("not a therapist", "do not diagnose", "three sentences", "one question"):
        assert phrase in SYSTEM_PROMPT


def test_memory_block_is_stable_when_empty():
    assert render_memory_block([]) == render_memory_block([])
    assert "slow" in render_memory_block(["prefers slow pacing"])
