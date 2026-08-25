"""The companion agent's harness (layer 3): a FastAPI app that runs one
turn per request and streams it back as SSE.

Deployed as a container on Lambda behind a Function URL, with the Lambda
Web Adapter bridging invocations to HTTP (docs/agent-runner-plan.md §1,
§5). Locally it is a plain uvicorn app. Everything about a turn's meaning
lives in ``agent``; this package owns identity, transport, the fencing
token's claim/commit/release around the engine, the deadline, and the
metrics.
"""
