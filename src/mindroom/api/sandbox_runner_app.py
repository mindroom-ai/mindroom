"""Minimal FastAPI app for sandbox runner sidecar."""

from fastapi import FastAPI

from mindroom.api.sandbox_runner import (
    _app_runtime_paths,
    _load_config_from_startup_runtime,
    ensure_registry_loaded_with_config,
)
from mindroom.api.sandbox_runner import router as sandbox_runner_router
from mindroom.config.main import load_config

app = FastAPI(title="MindRoom Sandbox Runner")
app.include_router(sandbox_runner_router)


@app.on_event("startup")
async def _startup() -> None:
    try:
        runtime_paths = _app_runtime_paths(app)
    except TypeError:
        runtime_paths, config = _load_config_from_startup_runtime()
        app.state.runtime_paths = runtime_paths
    else:
        config = load_config(runtime_paths) if runtime_paths.config_path.exists() else None
    ensure_registry_loaded_with_config(runtime_paths, config)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Minimal readiness/liveness probe for dedicated worker pods."""
    return {"status": "ok"}
