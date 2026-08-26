"""The turn as a graph: ``agent`` -> ``tools`` -> ``agent`` ... -> END.

The graph holds no decisions of its own. How many rounds a turn may run,
when the model is told to wrap up, when it is forced to propose and what
those hints say all come from ``agent.budget`` and ``agent.prompt`` -- the
same places the native loop reads them. What is different is the
mechanism: state carried by LangGraph instead of local variables, routing
by conditional edges instead of ``break``.

Recursion limit: a turn that uses every iteration runs ``agent`` and
``tools`` MAX_TOOL_ITERATIONS_PER_TURN times each and then ``agent`` once
more for the wrap-up answer (the call the native loop makes in its
``for ... else``): 2 * MAX + 1 node executions. LangGraph's limit counts
one step more than that -- the step that writes the input into the state
-- so the limit is 2 * MAX + 2, and the graph tests pin both numbers: one
lower fails the longest legitimate turn, and nothing legitimate needs more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph

from agent.budget import MAX_TOOL_ITERATIONS_PER_TURN, converge_policy
from agent.contracts import ContentBlock, Deadline, ForcedTool, ToolUseBlock
from agent.langgraph.messages import CACHE_POINT, content_blocks, stop_reason_of, usage_of
from agent.langgraph.tools import ToolRunner
from agent.model_ids import ModelFamily
from agent.prompt import NO_MORE_TOOLS_HINT

RECURSION_LIMIT = 2 * MAX_TOOL_ITERATIONS_PER_TURN + 2

# The custom event the tools node dispatches when a tool proposed; the
# engine turns it into ``ProposalReady`` in stream order.
PROPOSAL_EVENT = "proposal"


class TurnState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    # Model calls made so far in the loop proper (the wrap-up call is not
    # one, as in the native loop's count).
    iterations: int
    # Steering to append before the next model call, then cleared.
    hint: str | None
    # The call just made was the wrap-up: its tool calls are not honoured.
    wrap_up: bool


class TurnBudget:
    """What the nodes need that is not state: the wall clock, the turn's
    number (for the converge policy) and where usage accumulates."""

    def __init__(self, *, turn: int, deadline: Deadline) -> None:
        self.turn = turn
        self.deadline = deadline
        self.usage = usage_of(AIMessage(content=""))  # zero


RunTools = Callable[[list[ContentBlock], list[ToolUseBlock]], Awaitable[Any]]


def build_graph(
    model: BaseChatModel,
    tools: list[Any],
    *,
    system: SystemMessage,
    budget: TurnBudget,
    runner: ToolRunner,
    family: ModelFamily | None,
) -> CompiledStateGraph:
    bound_tools: list[Any] = list(tools)
    if family is ModelFamily.CLAUDE:
        # Claude caches the tool definitions when a breakpoint closes the
        # list; Nova rejects one there (agent.native.llm.converse says the
        # same). langchain-aws passes a cachePoint dict through untouched.
        bound_tools.append(dict(CACHE_POINT))

    async def agent(state: TurnState) -> dict[str, Any]:
        iterations = state["iterations"]
        out_of_time = budget.deadline.exhausted()
        wrap_up = out_of_time or iterations >= MAX_TOOL_ITERATIONS_PER_TURN
        hint = state["hint"] or (NO_MORE_TOOLS_HINT if wrap_up else None)
        steering: list[BaseMessage] = [HumanMessage(content=hint)] if hint else []

        # A forced choice applies to the first call only, and never once
        # the model is being asked for a plain answer.
        policy = converge_policy(budget.turn) if iterations == 0 and not out_of_time else "auto"
        tool_choice = policy.name if isinstance(policy, ForcedTool) else "auto"
        bound = model.bind_tools(bound_tools, tool_choice=tool_choice)

        reply = await bound.ainvoke([system, *state["messages"], *steering])
        assert isinstance(reply, AIMessage)
        budget.usage = budget.usage + usage_of(reply)
        return {
            "messages": [*steering, reply],
            "iterations": iterations + 1,
            "hint": None,
            "wrap_up": wrap_up,
        }

    async def tools_node(state: TurnState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        blocks = content_blocks(last)
        calls = [b for b in blocks if isinstance(b, ToolUseBlock)]
        executions = await runner.run(blocks, calls)
        results: list[BaseMessage] = []
        for execution in executions:
            results.append(
                ToolMessage(
                    content=[
                        {"type": "text", "text": part.text}
                        if hasattr(part, "text")
                        else {"type": "json", "json": part.data}  # type: ignore[union-attr]
                        for part in execution.result.content
                    ],
                    tool_call_id=execution.result.tool_use_id,
                    status=execution.result.status,
                )
            )
            if execution.proposal is not None:
                # Through the callback stream rather than a direct emit, so
                # it lands after the text the model streamed before it.
                await adispatch_custom_event(
                    PROPOSAL_EVENT, {"duration_minutes": execution.proposal.duration_minutes}
                )
        return {"messages": results}

    def after_agent(state: TurnState) -> Literal["tools", "__end__"]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        if stop_reason_of(last) != "tool_use" or not last.tool_calls:
            return END
        if state["wrap_up"]:
            # Asked for a plain answer and got tool calls anyway: the engine
            # answers with the text and drops them.
            return END
        return "tools"

    def after_tools(state: TurnState) -> Literal["agent", "__end__"]:  # noqa: ARG001
        return END if runner.finalized is not None else "agent"

    graph: StateGraph = StateGraph(TurnState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent, {"tools": "tools", END: END})
    graph.add_conditional_edges("tools", after_tools, {"agent": "agent", END: END})
    return graph.compile()
