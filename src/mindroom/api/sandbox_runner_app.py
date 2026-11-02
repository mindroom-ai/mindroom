"""Minimal FastAPI app for sandbox runner sidecar."""

import copy
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mindroom import __version__
from mindroom.api import sandbox_exec
from mindroom.api.sandbox_runner import (
    app_runner_token,
    app_runtime_config,
    app_runtime_paths,
    initialize_sandbox_runner_app,
    load_config_from_startup_runtime,
    startup_runner_token_from_env,
    validate_runner_token,
)
from mindroom.api.sandbox_runner import router as sandbox_runner_router
from mindroom.api.sandbox_runner_scripts import (
    prepare_script_worker_before_serving,
)
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.api.validation_errors import validation_error_response
from mindroom.api.worker_computer import router as worker_computer_router
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY, WORKER_COMPUTER_ENABLED_ENV
from mindroom.tool_system.worker_routing import resolved_worker_key_scope
from mindroom.worker_computer.display import WorkerDisplay
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.worker_computer.worker_browser import WorkerBrowserRuntime
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
        if not sandbox_exec.runner_uses_dedicated_worker(runtime_paths):
            msg = "Worker computer requires a dedicated worker."
            raise RuntimeError(msg)
        if os.name == "posix" and os.geteuid() == 0:
            msg = "Worker computer requires a non-root effective user."
            raise RuntimeError(msg)
        root = sandbox_exec.runner_dedicated_worker_root(runtime_paths)
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


class _RunnerTokenMiddleware:
    """Refuse requests without the runner token before any route reads their bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the token of every HTTP request except the health probe."""
        if scope["type"] == "http" and scope["path"] != "/healthz":
            request = Request(scope)
            try:
                await validate_runner_token(request, request.headers.get("x-mindroom-sandbox-token"))
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(
    title="MindRoom Sandbox Runner",
    lifespan=_lifespan,
    exception_handlers={RequestValidationError: validation_error_response},
)
app.add_middleware(_RunnerTokenMiddleware)
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


class _BelowWarning(logging.Filter):
    """Keep only records below WARNING."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether the record is informational."""
        return record.levelno < logging.WARNING


def _uvicorn_log_config() -> dict[str, Any]:
    """Return uvicorn's default logging with records below WARNING on stdout, like the runner's own records.

    Container log collectors read stderr as errors, so only uvicorn's warnings and errors go there.
    """
    config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
    handlers = config["handlers"]
    handlers["warnings"] = {**handlers["default"], "level": "WARNING"}
    handlers["default"] |= {"stream": "ext://sys.stdout", "filters": ["below_warning"]}
    config["filters"] = {"below_warning": {"()": _BelowWarning}}
    config["loggers"]["uvicorn"]["handlers"] = ["default", "warnings"]
    return config


def _main() -> None:
    """Serve the runner on every interface, at the configured runner port or 8766."""
    uvicorn.run(
        app,
        # Workers are reached over the container network; the runner token guards every route but the health probe.
        host="0.0.0.0",  # noqa: S104
        port=int(os.environ.get(SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"]) or 8766),
        log_config=_uvicorn_log_config(),
    )


if __name__ == "__main__":
    _main()
