"""The companion agent: a resumable loop over Bedrock tool calling.

Layers (docs/agent-runner-plan.md §1): ``native/`` is the hand-built engine
(LLM provider + loop), ``langgraph/`` will be the framework engine, and this
package's top level holds what both share -- the contract, the tools, the
prompt, the turn budget and the checkpoint bridge. Nothing here imports a
web framework or a database client: the harness (``agent_runner``) owns
transport and persistence.
"""
