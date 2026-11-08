"""A real sandbox runner for terminal tests, and a primary whose agents' shell commands run there."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import uvicorn

from mindroom.agents import build_agent_toolkit
from mindroom.api import sandbox_runner
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.workers.runtime import reset_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.agno_toolkits import AnyToolkit

RUNNER_TOKEN = "runner-token"  # noqa: S105
# Alice's turn of the agent `code` in a room.
IDENTITY = ToolExecutionIdentity("matrix", "code", "@alice:localhost", "!room:localhost", None, None, "session")


@pytest.fixture(autouse=True)
def _fresh_primary_worker_manager() -> Iterator[None]:
    reset_primary_worker_manager()
    yield
    reset_primary_worker_manager()


async def serve_runner(tmp_path: Path, execution_mode: str) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    """Serve the sandbox runner app on a free port, sharing the primary's storage, as a shared runner does."""
    seed = tmp_path / "seed" / "config.yaml"
    seed.parent.mkdir(exist_ok=True)
    seed.write_text("agents: {}\n", encoding="utf-8")
    runner_paths = resolve_runtime_paths(
        config_path=seed,
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": execution_mode,
        },
    )
    sandbox_runner.initialize_sandbox_runner_app(sandbox_runner_app, runner_paths, runner_token=RUNNER_TOKEN)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(sandbox_runner_app, ws="websockets-sansio", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    async with asyncio.timeout(30):
        while not server.started:
            if serving.done():
                await serving
            await asyncio.sleep(0.01)
    return server, serving, f"http://127.0.0.1:{listener.getsockname()[1]}"


@pytest_asyncio.fixture
async def runner(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[str]:
    """Return the URL of a running sandbox runner; ``request.param`` is its execution mode."""
    server, serving, url = await serve_runner(tmp_path, getattr(request, "param", "inprocess"))
    yield url
    server.should_exit = True
    await serving


def primary_paths(tmp_path: Path, runner_url: str, **env: str) -> RuntimePaths:
    """Return the paths of a primary whose agents' shell commands run in the runner at ``runner_url``."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_PROXY_URL": runner_url,
            "MINDROOM_SANDBOX_PROXY_TOKEN": RUNNER_TOKEN,
            "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
            "MINDROOM_SANDBOX_PROXY_TOOLS": "shell",
            **env,
        },
    )


def agent_toolkit(tool_name: str, config: Config, paths: RuntimePaths) -> AnyToolkit:
    """Build the agent `code`'s toolkit ``tool_name`` as its turn for ``IDENTITY`` builds it."""
    registry = builtin_tool_registry()
    toolkit = build_agent_toolkit(
        tool_name,
        agent_tools=resolve_agent_tools(config, "code", registry),
        placement=tool_placement(paths, registry),
        runtime_paths=paths,
        execution_identity=IDENTITY,
    )
    assert toolkit is not None
    return toolkit
