"""Minimal FastAPI app for sandbox runner sidecar."""

from fastapi import FastAPI

from mindroom.api.sandbox_runner import _load_config_from_env, ensure_registry_loaded_with_config
from mindroom.api.sandbox_runner import router as sandbox_runner_router

app = FastAPI(title="MindRoom Sandbox Runner")
app.include_router(sandbox_runner_router)


@app.on_event("startup")
async def _startup() -> None:
    runtime_paths, config = _load_config_from_env()
    ensure_registry_loaded_with_config(runtime_paths, config)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Minimal readiness/liveness probe for dedicated worker pods."""
    return {"status": "ok"}
