"""The ASGI app. ``uvicorn agent_runner.main:app`` locally; on Lambda the
Web Adapter runs the same command and streams the responses back."""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, Request

from agent_runner import deps as deps_module
from agent_runner.metrics import configure_logging
from agent_runner.routes import ROUTE_PREFIXES, router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="meditation companion", docs_url=None, redoc_url=None)
    # The same routes under both prefixes. The path is CloudFront's routing
    # key -- agent/* to the native function, agent-lg/* to the LangGraph one
    # (frontend_stack) -- and which engine answers is this function's
    # AGENT_ENGINE, never the path; a session pinned to the other engine is
    # refused with 409 wrong_engine (routes.py).
    for prefix in ROUTE_PREFIXES:
        app.include_router(router, prefix=prefix)

    @app.get("/health")
    def health() -> dict[str, str]:
        # Reachable without configuration so a container starts green;
        # the engine and model are reported once the deps exist.
        info = {"status": "ok"}
        try:
            deps = deps_module.get_deps()
        except RuntimeError:
            return info
        return {**info, "engine": deps.settings.engine, "model_id": deps.provider.model_id}

    @app.middleware("http")
    async def access_log(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        response = await call_next(request)
        # Path template, never the body: session ids are fine, user text is not.
        logger.info(
            "%s %s -> %d %dms",
            request.method,
            request.url.path,
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    return app


configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
app = create_app()
