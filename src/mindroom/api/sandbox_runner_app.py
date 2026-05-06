"""Minimal FastAPI app for sandbox runner sidecar."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mindroom.api.sandbox_runner import (
    app_runner_token,
    app_runtime_config,
    app_runtime_paths,
    initialize_sandbox_runner_app,
    load_config_from_startup_runtime,
    startup_runner_token_from_env,
)
from mindroom.api.sandbox_runner import router as sandbox_runner_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        runtime_paths = app_runtime_paths(app)
    except TypeError:
        runtime_paths, config = load_config_from_startup_runtime()
        runner_token = startup_runner_token_from_env()
    else:
        config = app_runtime_config(app)
        runner_token = app_runner_token(app)
    initialize_sandbox_runner_app(
        app,
        runtime_paths,
        config=config,
        runner_token=runner_token,
    )
    yield


app = FastAPI(title="MindRoom Sandbox Runner", lifespan=_lifespan)
app.include_router(sandbox_runner_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Minimal readiness/liveness probe for dedicated worker pods."""
    return {"status": "ok"}
