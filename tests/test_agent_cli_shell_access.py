"""Minimal Bash runs where the agent's shell runs and calls MindRoom back from there."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI

from mindroom import ai
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_cli.shell_access import agent_cli_shell_env, minimal_shell_problems
from mindroom.api.agent_cli import bind_agent_cli_registry, router
from mindroom.config.agent import AgentConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.identity_helpers import persist_entity_accounts
from tests.minimal_agent_fixtures import ScriptedProvider
from tests.test_agent_cli_authority import _runtime_context, _turn_context

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from mindroom.constants import RuntimePaths

_COMMAND = (
    # A `mindroom` directory where the agent works, like a checkout, must not replace the installed CLI.
    "mkdir -p checkout/mindroom && echo 'raise SystemExit(9)' > checkout/mindroom/__init__.py && cd checkout && "
    "mindroom-agent tools list && "
    'printf "path=%s\\n" "$PATH" && '
    'printf "pass=%s\\n" "$PASSTHROUGH_PROBE"'
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
    """Without workers, the response's Bash finds `mindroom-agent` and reaches the API with its grant."""
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
    # The agent's own shell settings still apply, like in its ordinary local shell.
    assert "pass=visible" in output, output
    # Only `mindroom-agent` is added, from a private directory, not MindRoom's whole environment.
    bin_dir = Path(output.split("path=", 1)[1].splitlines()[0].split(os.pathsep)[0])
    assert [path.name for path in bin_dir.iterdir()] == ["mindroom-agent"]
    # Outside every agent directory that worker code could write.
    assert not bin_dir.is_relative_to(runtime.runtime_paths.storage_root / "agents")
    # The grant dies with the response.
    assert not running_api._owners


@pytest.fixture
def api_address() -> Iterator[None]:
    """Run like `mindroom run`, which serves its API on every interface by default."""
    set_api_server_address("0.0.0.0", 8765)  # noqa: S104 - the default bind address
    yield
    clear_api_server_address()


def _worker_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_API_KEY": "dashboard-key", "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test", **env},
    )


@pytest.mark.usefixtures("api_address")
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MINDROOM_WORKER_BACKEND": "docker"}, "http://host.docker.internal:8765"),
        (
            {"MINDROOM_WORKER_BACKEND": "kubernetes", "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://mindroom:8765/"},
            "http://mindroom:8765",
        ),
    ],
)
def test_worker_shells_call_back_through_the_address_workers_reach(
    tmp_path: Path,
    env: dict[str, str],
    expected: str,
) -> None:
    """The agent's ordinary worker gets the API as the worker reaches it, with no launcher directory."""
    config = _runtime_context(tmp_path).config
    paths = _worker_paths(tmp_path, **env)

    assert minimal_shell_problems(config, paths, "helper") == []
    shell_env = agent_cli_shell_env(config, paths, "helper", "grant")
    assert shell_env.env() == {"MINDROOM_AGENT_CLI_URL": expected, "MINDROOM_AGENT_CLI_TOKEN": "grant"}
    assert shell_env.bin_dir is None


@pytest.mark.usefixtures("api_address")
def test_worker_shells_without_a_known_address_or_key_list_every_fix(tmp_path: Path) -> None:
    """Only Docker maps a wildcard bind to the host, and worker shells need a protected API."""
    config = _runtime_context(tmp_path).config
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes"},
    )

    problems = minimal_shell_problems(config, paths, "helper")

    assert [problem.split("`")[1] for problem in problems] == ["MINDROOM_API_KEY", "MINDROOM_AGENT_CLI_PRIMARY_URL"]


@pytest.mark.parametrize("worker", [False, True], ids=["local", "worker"])
def test_minimal_bash_needs_the_running_api(tmp_path: Path, *, worker: bool) -> None:
    """Only this process's API serves the grants, wherever the shell runs, even with an explicit URL."""
    runtime = _runtime_context(tmp_path)
    paths = (
        _worker_paths(tmp_path, MINDROOM_WORKER_BACKEND="docker", MINDROOM_AGENT_CLI_PRIMARY_URL="http://mindroom:8765")
        if worker
        else runtime.runtime_paths
    )

    assert minimal_shell_problems(runtime.config, paths, "helper") == [
        "Run MindRoom with its API server (without `--no-api`), because the CLI calls back through it.",
    ]


@pytest.mark.usefixtures("api_address")
def test_local_launcher_directory_is_private_to_mindroom(tmp_path: Path) -> None:
    """The directory first on the shell's PATH is in MindRoom's storage, not shared temp another local user could refill."""
    runtime = _runtime_context(tmp_path)
    storage_root = runtime.runtime_paths.storage_root
    first = agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir
    assert first is not None
    bin_dir = Path(first)
    assert bin_dir.parent == storage_root
    assert stat.S_IMODE(bin_dir.stat().st_mode) == 0o700
    # A launcher an earlier installation left there runs that installation's interpreter.
    launcher = bin_dir / "mindroom-agent"
    launcher.write_text("#!/bin/sh\nexec /old/venv/bin/python\n")

    assert agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir == first
    assert "/old/venv" not in launcher.read_text()
    assert os.access(launcher, os.X_OK)


@pytest.mark.usefixtures("api_address")
def test_local_launcher_is_recreated_after_removal(tmp_path: Path) -> None:
    """A long-running MindRoom makes the launcher again after something removes it."""
    runtime = _runtime_context(tmp_path)
    first = agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir
    assert first is not None
    shutil.rmtree(first)

    second = agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir

    assert second is not None
    assert os.access(Path(second) / "mindroom-agent", os.X_OK)


@pytest.mark.usefixtures("api_address")
def test_local_launcher_is_made_executable_again(tmp_path: Path) -> None:
    """A launcher with the right content that lost its exec bit would fail every minimal command."""
    runtime = _runtime_context(tmp_path)
    first = agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir
    assert first is not None
    launcher = Path(first) / "mindroom-agent"
    launcher.chmod(0o644)

    assert agent_cli_shell_env(runtime.config, runtime.runtime_paths, "helper", "grant").bin_dir == first
    assert os.access(launcher, os.X_OK)
