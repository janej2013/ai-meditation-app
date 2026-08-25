"""The eval cases are well-formed; running them is a manual step."""

from __future__ import annotations

from agent.budget import FINALIZE_TOOL_NAME

from .evals.cases import CASES, Case


def test_case_names_are_unique():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


def test_case_shapes():
    assert len(CASES) >= 20
    for case in CASES:
        assert isinstance(case, Case)
        assert case.turns and all(t.strip() for t in case.turns)
        e = case.expect
        assert not (set(e.must_call) & set(e.must_not_call)), case.name
        if e.max_turns_to_finalize is not None:
            assert e.must_finalize, case.name
            assert e.max_turns_to_finalize <= len(case.turns), case.name
        if e.must_finalize:
            assert FINALIZE_TOOL_NAME not in e.must_not_call, case.name


def test_crisis_cases_forbid_every_tool():
    crisis = [c for c in CASES if c.name.startswith("crisis-") and c.name != "crisis-then-return"]
    assert len(crisis) >= 4
    for case in crisis:
        assert FINALIZE_TOOL_NAME in case.expect.must_not_call
        assert "13 11 14" in case.expect.must_contain
