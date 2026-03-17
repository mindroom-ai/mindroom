"""Minimal FastAPI app for sandbox runner sidecar."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mindroom.api.sandbox_runner import (
    _app_runtime_paths,
    _load_config_from_startup_runtime,
    _runtime_config_or_empty,
    _startup_runner_token_from_env,
    ensure_registry_loaded_with_config,
    initialize_sandbox_runner_app,
)
from mindroom.api.sandbox_runner import router as sandbox_runner_router


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        runtime_paths = _app_runtime_paths(app)
    except TypeError:
        runtime_paths, config = _load_config_from_startup_runtime()
        initialize_sandbox_runner_app(app, runtime_paths, runner_token=_startup_runner_token_from_env())
    else:
        config = _runtime_config_or_empty(runtime_paths)
    ensure_registry_loaded_with_config(runtime_paths, config)
    yield


app = FastAPI(title="MindRoom Sandbox Runner", lifespan=_lifespan)
app.include_router(sandbox_runner_router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Minimal readiness/liveness probe for dedicated worker pods."""
    return {"status": "ok"}
