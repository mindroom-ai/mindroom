"""Minimal FastAPI app for sandbox runner sidecar."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mindroom import __version__
from mindroom.api import sandbox_exec
from mindroom.api.sandbox_runner import (
    app_runner_token,
    app_runtime_config,
    app_runtime_paths,
    initialize_sandbox_runner_app,
    load_config_from_startup_runtime,
    startup_runner_token_from_env,
)
from mindroom.api.sandbox_runner import router as sandbox_runner_router
from mindroom.api.sandbox_runner_scripts import (
    prepare_script_worker_before_serving,
)
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.api.worker_computer import router as worker_computer_router
from mindroom.runtime_env_policy import WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.worker_routing import resolved_worker_key_scope
from mindroom.worker_browser import WorkerBrowserRuntime
from mindroom.worker_computer.display import WorkerDisplay
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.workers.compatibility import worker_health_payload


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        runtime_paths = app_runtime_paths(app)
    except TypeError:
        runner_token = startup_runner_token_from_env()
        runtime_paths, config = load_config_from_startup_runtime()
    else:
        config = app_runtime_config(app)
        runner_token = app_runner_token(app)
    initialize_sandbox_runner_app(
        app,
        runtime_paths,
        config=config,
        runner_token=runner_token,
    )
    computer = None
    if runtime_paths.env_flag(WORKER_COMPUTER_ENABLED_ENV):
        root = sandbox_exec.runner_dedicated_worker_root(runtime_paths)
        if not sandbox_exec.runner_uses_dedicated_worker(runtime_paths) or root is None:
            msg = "Worker computer requires a dedicated worker."
            raise RuntimeError(msg)
        computer = WorkerComputerRuntime(WorkerDisplay(root / ".computer"))
    app.state.worker_computer = computer
    dedicated_key = sandbox_exec.runner_dedicated_worker_key(runtime_paths)
    browser = (
        WorkerBrowserRuntime()
        if computer is None
        and dedicated_key is not None
        and resolved_worker_key_scope(dedicated_key) in {"shared", "user", "user_agent"}
        else None
    )
    app.state.worker_browser = browser
    try:
        await prepare_script_worker_before_serving(app)
        yield
    finally:
        if computer is not None:
            await computer.close()
        if browser is not None:
            await browser.close()


app = FastAPI(title="MindRoom Sandbox Runner", lifespan=_lifespan)
app.include_router(sandbox_runner_router)
app.include_router(worker_computer_router)
app.include_router(sandbox_runner_scripts_router)


@app.get("/healthz")
async def healthz() -> dict[str, str | int]:
    """Return readiness plus the worker compatibility contract."""
    payload = worker_health_payload(mindroom_version=__version__)
    return {
        "status": payload.status,
        "mindroom_version": payload.mindroom_version,
        "worker_protocol": payload.worker_protocol,
    }
