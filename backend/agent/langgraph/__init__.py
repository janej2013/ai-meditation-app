"""The framework engine: LangChain messages and a LangGraph graph over the
same tools, prompt and checkpoint format as ``agent.native``.

Nothing outside this package imports LangChain or LangGraph; the harness
reaches it through ``agent_runner.turns.build_engine`` with a lazy import,
so a deployment that never selects it never needs the dependency.
"""
