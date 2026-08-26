"""The graph's shape and its limits.

The mermaid snapshot is the graph's structure as a fixture: change the
graph and the fixture must change with it, deliberately. The recursion
limit is pinned from both sides -- the longest legitimate turn fits, one
step less does not -- because a limit that is merely "big enough" hides
a loop that never ends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.budget import MAX_TOOL_ITERATIONS_PER_TURN
from agent.contracts import Deadline, TurnInput
from agent.tools.registry import ToolContext

from .fake_provider import run, text_reply, tool_reply
from .scenario_tools import USER, Collector, registry

pytest.importorskip("langgraph")

from langgraph.errors import GraphRecursionError

import agent.langgraph.engine as engine_module
from agent.langgraph.engine import LangGraphEngine
from agent.langgraph.graph import RECURSION_LIMIT, TurnBudget, build_graph
from agent.langgraph.messages import system_message
from agent.langgraph.tools import ToolRunner, bound_tools

from .fake_chat_model import ScriptedChatModel

FIXTURE = Path(__file__).parent / "fixtures" / "langgraph_graph.mmd"


def compiled():
    reg = registry()
    return build_graph(
        ScriptedChatModel(),
        bound_tools(reg),
        system=system_message([]),
        budget=TurnBudget(turn=0, deadline=Deadline.never()),
        runner=ToolRunner(reg, ToolContext(user_id="u", session_id="s")),
        family=None,
    )


def test_graph_shape_matches_the_snapshot():
    assert compiled().get_graph().draw_mermaid() == FIXTURE.read_text()


def longest_turn() -> list[list]:
    """Every iteration ends in a tool call; the wrap-up call answers."""
    rounds = [tool_reply(("noop", {}, f"tu-{i}")) for i in range(MAX_TOOL_ITERATIONS_PER_TURN)]
    return [*rounds, text_reply("closing")]


def run_with_limit(limit: int):
    model = ScriptedChatModel(script=longest_turn())
    engine = LangGraphEngine(model, registry(), USER, system_prompt="S")
    original = engine_module.RECURSION_LIMIT
    engine_module.RECURSION_LIMIT = limit
    try:
        return run(
            engine.run_turn(
                TurnInput(history=[], user_text="go", turn=0),
                deadline=Deadline.never(),
                emit=Collector(),
            )
        )
    finally:
        engine_module.RECURSION_LIMIT = original


def test_the_limit_fits_the_longest_turn_exactly():
    result = run_with_limit(RECURSION_LIMIT)
    assert len(result.tool_log) == MAX_TOOL_ITERATIONS_PER_TURN

    with pytest.raises(GraphRecursionError):
        run_with_limit(RECURSION_LIMIT - 1)


def test_the_limit_is_derived_from_the_iteration_cap():
    # 2 node executions per iteration, one wrap-up call, one step LangGraph
    # counts for writing the input.
    assert RECURSION_LIMIT == 2 * MAX_TOOL_ITERATIONS_PER_TURN + 2
