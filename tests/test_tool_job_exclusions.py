"""Self-managed shell operations keep their native lifetime and control handles."""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.shell_execution import discard_background_record
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tools.shell import _process_registry
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.run.agent import ToolExecution
    from agno.tools import Toolkit

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

type _ShellRuntime = tuple[Agent | Team, DelegationModel, ToolJobRuntime, ToolExecutionIdentity, Toolkit]


@pytest_asyncio.fixture(
    params=[(False, "shell"), (True, "shell"), (False, "openclaw_compat"), (True, "openclaw_compat")],
    ids=["agent", "team", "preset-agent", "preset-team"],
)
async def shell_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[_ShellRuntime]:
    """Use registered, output-wrapped shell tools through actual SDK dispatch."""
    paths = _runtime_paths(tmp_path)
    team, authored_name = request.param
    config = Config(
        background_tool_jobs=True,
        agents={"leader": AgentConfig(display_name="Leader", tools=[authored_name])},
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "shell",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tool_output_workspace_root=tmp_path,
    )
    bind_toolkit_authority(toolkit, authored_name=authored_name)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    existing_handles = set(_process_registry)
    actor = (
        Team(id="leader", model=model, tools=[toolkit], members=[], telemetry=False)
        if team
        else Agent(id="leader", model=model, tools=[toolkit], telemetry=False)
    )
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                yield actor, model, runtime, owner, toolkit
    finally:
        (tmp_path / "release").touch()
        register_background_runtime(paths, None)
        await runtime.shutdown()
        for handle in set(_process_registry) - existing_handles:
            task = _process_registry[handle]._monitor_task
            discard_background_record(_process_registry, handle)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)


async def _invoke(actor: Agent | Team, model: DelegationModel, name: str, **arguments: object) -> ToolExecution:
    model.responses.extend(
        [
            ModelResponse(tool_calls=[_call(name, f"call-{name}", **arguments)]),
            ModelResponse(content="done"),
        ],
    )
    response = await actor.arun("Execute the tool", session_id="session")
    assert response.tools is not None
    return response.tools[0]


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text():  # noqa: ASYNC110 - Readiness belongs to another process.
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_shell_native_handles_control_the_actual_process(
    tmp_path: Path,
    shell_runtime: _ShellRuntime,
    cancel: bool,
) -> None:
    """Native timeouts, polling, and cancellation never create misleading completed jobs."""
    actor, model, runtime, owner, toolkit = shell_runtime
    try:
        started = await _invoke(
            actor,
            model,
            "run_shell_command",
            timeout=0,
            args="echo $$ > started; while [ ! -f release ]; do sleep 0.01; done; printf 'native result'",
        )
        assert not started.tool_call_error, started.result
        match = re.search(r"Handle: (shell:[a-f0-9]+)", started.result)
        assert match is not None, started.result
        handle = match.group(1)
        await _wait_for_file(tmp_path / "started")
        pid = int((tmp_path / "started").read_text())
        os.kill(pid, 0)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
        running = await _invoke(actor, model, "check_shell_command", handle=handle)
        assert "Status: RUNNING" in running.result
        if cancel:
            killed = await _invoke(actor, model, "kill_shell_command", handle=handle, force=True)
            assert "Force-killed" in killed.result
        else:
            (tmp_path / "release").touch()
        async with asyncio.timeout(5):
            while True:
                completed = await _invoke(actor, model, "check_shell_command", handle=handle)
                if "Status: RUNNING" not in completed.result:
                    break
                await asyncio.sleep(0.01)
        if not cancel:
            assert "native result" in completed.result
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
        for function in toolkit.get_async_functions().values():
            schema = model._format_tools([function])[0]["function"]["parameters"]
            assert "wait_timeout" not in schema["properties"]
        run_schema = model._format_tools([toolkit.get_async_functions()["run_shell_command"]])[0]["function"]
        assert "timeout" in run_schema["parameters"]["properties"]
        assert "mindroom_output_path" in run_schema["parameters"]["properties"]
    finally:
        (tmp_path / "release").touch()


@pytest.mark.asyncio
async def test_shell_rejects_extra_wait_before_side_effect(tmp_path: Path, shell_runtime: _ShellRuntime) -> None:
    """A stale model call cannot silently create a second shell execution owner."""
    actor, model, runtime, owner, _ = shell_runtime
    result = await _invoke(actor, model, "run_shell_command", args="echo wrong > unexpected", wait_timeout=0)
    assert result.tool_call_error
    assert "wait_timeout" in result.result
    assert not (tmp_path / "unexpected").exists()
    assert await runtime.list_jobs(owner=owner, depth=0) == []


@pytest.mark.asyncio
async def test_human_followup_keeps_shell_under_native_wait(tmp_path: Path, shell_runtime: _ShellRuntime) -> None:
    """A human signal cannot wrap an excluded process in a second background handle."""
    actor, model, runtime, owner, _ = shell_runtime
    signal = HumanMessageSignal()
    with human_message_signal_context(signal):
        pending = asyncio.create_task(
            _invoke(
                actor,
                model,
                "run_shell_command",
                timeout=5,
                args="echo started > started; while [ ! -f release ]; do sleep 0.01; done; printf 'native result'",
            ),
        )
        try:
            await _wait_for_file(tmp_path / "started")
            signal.notify()
            done, _ = await asyncio.wait({pending}, timeout=0.05)
            assert not done
            assert await runtime.list_jobs(owner=owner, depth=0) == []
            (tmp_path / "release").touch()
            result = await asyncio.wait_for(pending, 5)
            assert "native result" in result.result
            assert await runtime.list_jobs(owner=owner, depth=0) == []
        finally:
            (tmp_path / "release").touch()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_named_unrelated_function_still_backgrounds(shell_runtime: _ShellRuntime) -> None:
    """Exclusions identify the toolkit as well as the function name."""
    actor, model, runtime, owner, _ = shell_runtime
    release = asyncio.Event()

    async def run_shell_command() -> str:
        await release.wait()
        return "unrelated result"

    actor.tools = [run_shell_command]
    try:
        result = await _invoke(actor, model, "run_shell_command", wait_timeout=0)
        handle = json.loads(result.result)
        assert handle["status"] == "running"
        release.set()
        completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
        assert completed.job.result == "unrelated result"
    finally:
        release.set()
