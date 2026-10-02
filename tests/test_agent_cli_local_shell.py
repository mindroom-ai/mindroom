"""Minimal mode runs Bash in MindRoom itself for agents whose shell already runs there."""

from __future__ import annotations

import asyncio
import signal
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI

from mindroom import ai
from mindroom.agent_cli.local_shell import LocalCliShell, shell_runs_in_primary
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_cli.worker_protocol import CliShellSettings
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.config.agent import AgentConfig
from mindroom.constants import DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES, DEFAULT_TOOL_OUTPUT_MAX_BYTES
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tools import shell as shell_tool_module
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_COMMAND = (
    "mindroom-agent tools list && "
    'printf "pass=%s\\n" "$PASSTHROUGH_PROBE" && '
    'printf "token=%s\\n" "$MINDROOM_AGENT_CLI_TOKEN_PATH" && '
    'stat -c "mode=%a" "$MINDROOM_AGENT_CLI_TOKEN_PATH"'
)


@pytest_asyncio.fixture
async def running_api() -> AsyncIterator[TurnToolRegistry]:
    """Serve the real CLI routes like `mindroom run`, on a loopback port the shell can call."""
    registry = TurnToolRegistry()
    app = FastAPI()
    app.include_router(router)
    bind_agent_cli_registry(app, registry)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="none"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes startup only as a flag
        await asyncio.sleep(0.01)
    set_api_server_address("127.0.0.1", sock.getsockname()[1])
    try:
        yield registry
    finally:
        clear_api_server_address()
        server.should_exit = True
        await serving
        sock.close()


@pytest.mark.asyncio
async def test_local_minimal_bash_runs_the_real_cli_against_the_running_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    running_api: TurnToolRegistry,
) -> None:
    """Without workers, the response's Bash finds `mindroom-agent`, reaches the API, and keeps its grant private."""
    runtime = _runtime_context(tmp_path)
    runtime.config.agents["helper"] = AgentConfig(
        display_name="Helper",
        tools=[{"shell": {"extra_env_passthrough": ["PASSTHROUGH_PROBE"]}}, "calculator"],
        memory_backend="file",
        learning=False,
    )
    runtime = replace(
        runtime,
        orchestrator=SimpleNamespace(agent_cli_registry=running_api),
        runtime_paths=replace(
            runtime.runtime_paths,
            # Arbitrary process variables reach shells only through the agent's passthrough setting.
            process_env={**runtime.runtime_paths.process_env, "PASSTHROUGH_PROBE": "visible"},
        ),
    )
    persist_entity_accounts(runtime.config, runtime.runtime_paths)
    assert shell_runs_in_primary(runtime.config, runtime.runtime_paths, "helper")
    provider = ScriptedProvider()
    provider.install(monkeypatch)
    provider.steps = [[("bash", {"command": _COMMAND})], "done"]

    with tool_runtime_context(runtime):
        result = await ai.ai_response(
            replace(_turn_context(), agent_mode="minimal", run_id=None),
            prompt="List your tools",
            runtime_paths=runtime.runtime_paths,
            config=runtime.config,
            execution_identity=build_execution_identity_from_runtime_context(runtime),
            supports_native_tool_approval=True,
        )

    assert "done" in result
    assert [tool["function"]["name"] for tool in provider.requests[0]["tools"]] == ["bash"]
    output = next(str(message["content"]) for message in provider.requests[1]["messages"] if message["role"] == "tool")
    assert '"toolkit":"calculator"' in output.replace(" ", ""), output
    assert "mode=600" in output, output
    # The agent's own shell settings still apply, like in its ordinary local shell.
    assert "pass=visible" in output, output
    token_path = Path(output.split("token=", 1)[1].splitlines()[0])
    # The grant file lives outside the workspace and disappears with the response.
    assert not token_path.is_relative_to(tmp_path)
    assert not token_path.exists()
    assert not running_api._owners


@pytest.mark.asyncio
async def test_response_end_stops_only_its_own_background_commands(tmp_path: Path) -> None:
    """Nothing can reach a finished response's handles, so they stop with it, like a retired worker's."""
    set_api_server_address("127.0.0.1", 8765)
    shells = []
    try:
        for turn in ("first", "second"):
            shell = LocalCliShell(_runtime_context(tmp_path).runtime_paths)
            bridge = SimpleNamespace(owner=SimpleNamespace(turn_id=turn, generation="run"), revoke=lambda: None)
            settings = CliShellSettings(
                workspace=str(tmp_path),
                shell_path_prepend=None,
                output_max_bytes=DEFAULT_TOOL_OUTPUT_MAX_BYTES,
                output_auto_save_threshold_bytes=DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
            )
            grant = SimpleNamespace(raw_token=f"grant-{turn}")
            await shell.install_grant(bridge, grant, shell=settings)  # type: ignore[arg-type]
            result = str(await shell.invoke_shell("run_shell_command", {"args": "sleep 30", "timeout": 1}))
            handle = next(line.split(":", 1)[1].strip() for line in result.splitlines() if line.startswith("Handle:"))
            shells.append((shell, handle))
        (first, first_handle), (second, second_handle) = shells
        first_process = shell_tool_module._process_registry[first_handle].process
        first.close()
        assert first_handle not in shell_tool_module._process_registry
        assert await asyncio.wait_for(first_process.wait(), timeout=5) == -signal.SIGKILL
        assert "RUNNING" in str(await second.invoke_shell("check_shell_command", {"handle": second_handle}))
    finally:
        clear_api_server_address()
        for shell, _handle in shells:
            shell.close()
