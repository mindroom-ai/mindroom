"""Excluded toolkits keep their native lifetime instead of becoming background jobs."""

from __future__ import annotations

import asyncio
import json
import os
from textwrap import dedent
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import isolated_plugin_runtime
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from tests.conftest import test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.test_config_lifecycle import _make_lifecycle
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    job_actor,
    job_tool,
    run_job_turn,
    tool_job_runtime,
    toolkit_job_tools,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_system.agno_toolkits import Toolkit
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


type _ShellRuntime = tuple[JobActor, ToolJobRuntime, ToolExecutionIdentity, Toolkit]


@pytest_asyncio.fixture(params=["shell", "openclaw_compat"], ids=["shell", "preset"])
async def shell_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> AsyncIterator[_ShellRuntime]:
    """Use registered, output-wrapped shell tools through the owned loop's dispatch."""
    paths = _runtime_paths(tmp_path)
    authored_name = request.param
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["shell"]),
        agents={"leader": AgentConfig(display_name="Leader", tools=[authored_name])},
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    toolkit = get_tool_by_name(
        "shell",
        paths,
        worker_target=None,
        disable_sandbox_proxy=True,
        tool_init_overrides={"base_dir": str(tmp_path)},
        tool_output_workspace_root=tmp_path,
    )
    actor = job_actor(*toolkit_job_tools(toolkit, authored_name=authored_name).values())
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                yield actor, runtime, owner, toolkit
    finally:
        (tmp_path / "release").touch()
        await runtime.shutdown()


async def _invoke(
    actor: JobActor,
    name: str,
    **arguments: object,
) -> tuple[ToolExecution, Mapping[str, Mapping[str, Any]]]:
    """Run one reply that calls ``name``: the call's execution, and each offered function's schema as the model saw it."""
    model = ScriptedModel([calls(tool_call(name, f"call-{name}", **arguments)), answer("done")])
    record = await run_job_turn(actor, model, "Execute the tool")
    assert record.tools is not None
    return record.tools[0], {definition.name: definition.parameters for definition in model.requests[0].tools}


async def _wait_for_file(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists() or not path.read_text():  # noqa: ASYNC110 - Readiness belongs to another process.
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_an_excluded_shell_command_stops_at_its_timeout_without_a_job(
    tmp_path: Path,
    shell_runtime: _ShellRuntime,
) -> None:
    """An excluded shell keeps its own timeout, stops the command there, and never creates a job."""
    actor, runtime, owner, toolkit = shell_runtime
    try:
        stopped, schemas = await _invoke(
            actor,
            "run_shell_command",
            timeout=1,
            args="echo $$ > started; while [ ! -f release ]; do sleep 0.01; done; printf 'native result'",
        )
        assert not stopped.tool_call_error, stopped.result
        assert str(stopped.result).endswith("Stopped after 1s.")
        with pytest.raises(ProcessLookupError):
            os.kill(int((tmp_path / "started").read_text()), 0)
        assert await runtime.list_jobs(owner=owner, depth=0) == []
        for name in toolkit.get_async_functions():
            assert "background_after" not in schemas[name]["properties"]
        run_schema = schemas["run_shell_command"]
        assert "timeout" in run_schema["properties"]
        assert "mindroom_output_path" in run_schema["properties"]
    finally:
        (tmp_path / "release").touch()


@pytest.mark.asyncio
async def test_empty_exclusion_list_includes_shell(shell_runtime: _ShellRuntime) -> None:
    """An empty exclusion list makes shell calls background jobs in execution and schemas."""
    actor, runtime, owner, _ = shell_runtime
    context = get_tool_runtime_context()
    assert context is not None
    context.config.background_tool_jobs.exclude_toolkits.clear()
    # Start the instance again with the explicit empty list.
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    result, schemas = await _invoke(actor, "run_shell_command", args="printf 'managed shell'")
    assert not result.tool_call_error
    assert "managed shell" in str(result.result)
    assert len(await runtime.list_jobs(owner=owner, depth=0)) == 1
    assert "background_after" in schemas["run_shell_command"]["properties"]


@pytest.mark.asyncio
async def test_shell_rejects_extra_wait_before_side_effect(tmp_path: Path, shell_runtime: _ShellRuntime) -> None:
    """A stale model call cannot silently create a second shell execution owner."""
    actor, runtime, owner, _ = shell_runtime
    result, _ = await _invoke(actor, "run_shell_command", args="echo wrong > unexpected", background_after=0)
    assert result.tool_call_error
    assert "background_after" in str(result.result)
    assert not (tmp_path / "unexpected").exists()
    assert await runtime.list_jobs(owner=owner, depth=0) == []


@pytest.mark.asyncio
async def test_human_followup_keeps_shell_under_native_wait(tmp_path: Path, shell_runtime: _ShellRuntime) -> None:
    """A human signal cannot wrap an excluded process in a second background handle."""
    actor, runtime, owner, _ = shell_runtime
    signal = asyncio.Event()
    with pending_human_messages_context(signal):
        pending = asyncio.create_task(
            _invoke(
                actor,
                "run_shell_command",
                timeout=5,
                args="echo started > started; while [ ! -f release ]; do sleep 0.01; done; printf 'native result'",
            ),
        )
        try:
            await _wait_for_file(tmp_path / "started")
            signal.set()
            done, _ = await asyncio.wait({pending}, timeout=0.05)
            assert not done
            assert await runtime.list_jobs(owner=owner, depth=0) == []
            (tmp_path / "release").touch()
            result, _ = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
            assert "native result" in str(result.result)
            assert await runtime.list_jobs(owner=owner, depth=0) == []
        finally:
            (tmp_path / "release").touch()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_named_unrelated_function_still_backgrounds(shell_runtime: _ShellRuntime) -> None:
    """Exclusions identify the toolkit as well as the function name."""
    _, runtime, owner, _ = shell_runtime
    release = asyncio.Event()

    async def run_shell_command() -> str:
        await release.wait()
        return "unrelated result"

    try:
        result, _ = await _invoke(job_actor(job_tool(run_shell_command)), "run_shell_command", background_after=0)
        handle = json.loads(str(result.result))
        assert handle["status"] == "running"
        release.set()
        completed = await runtime.wait(handle["job_id"], owner=owner, depth=0)
        assert completed.job.result == "unrelated result"
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded_name", [None, "native_plugin", "different_runtime_name"])
async def test_registered_plugin_exclusion_is_pinned_for_every_function(  # noqa: PLR0915 - Real plugin construction and restart policy.
    tmp_path: Path,
    excluded_name: str | None,
) -> None:
    """YAML excludes complete registered toolkits, preserving native args across reload until restart."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(json.dumps({"name": "native-demo", "tools_module": "tools.py"}))
    (plugin / "tools.py").write_text(
        dedent("""\
        from pathlib import Path
        from agno.tools import Toolkit
        from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
        from mindroom.tool_system.registration import register_tool_with_metadata

        class NativeTools(Toolkit):
            def __init__(self):
                super().__init__(name="different_runtime_name", tools=[self.native_step, self.native_status])

            async def native_step(self, background_after: int = 7) -> str:
                Path(__file__).with_name("executions").write_text(f"step:{background_after}\\n")
                return f"step:{background_after}"

            async def native_status(self) -> str:
                with Path(__file__).with_name("executions").open("a") as output:
                    output.write("status\\n")
                return "status"

        @register_tool_with_metadata(name="native_plugin", display_name="Native", description="Native execution", category=ToolCategory.DEVELOPMENT, file_access=ToolFileAccess.NONE)
        def native_tools():
            return NativeTools
    """),
    )
    config = Config.model_validate(
        {
            "background_tool_jobs": {"enabled": True, "exclude_toolkits": [excluded_name] if excluded_name else []},
            "agents": {"leader": {"display_name": "Leader", "tools": ["native_plugin"]}},
            "plugins": [str(plugin)],
        },
    )
    paths = test_runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    instance = pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    excluded = excluded_name == "native_plugin"
    try:
        with isolated_plugin_runtime(config, paths), tool_runtime_context(context):
            toolkit = get_tool_by_name("native_plugin", paths, runtime_config=config, worker_target=None)
            actor = job_actor(*toolkit_job_tools(toolkit, authored_name="native_plugin").values())
            async with execution_resources():
                if not excluded:
                    rejected, _ = await _invoke(actor, "native_step", background_after=3)
                    assert rejected.tool_call_error
                    assert "exclude_toolkits" in str(rejected.result)
                    assert not (plugin / "executions").exists()
                    assert await runtime.list_jobs(owner=owner, depth=0) == []
                    return
                # Editing the authored list must not mutate the startup snapshot.
                config.background_tool_jobs.exclude_toolkits.clear()
                lifecycle = _make_lifecycle(tmp_path, current_config=config)
                lifecycle.record_applied(config)
                assert lifecycle.status.status == "restart_required"
                step, _ = await _invoke(actor, "native_step", background_after=3)
                assert not step.tool_call_error
                assert step.result == "step:3"
                status, schemas = await _invoke(actor, "native_status")
                assert not status.tool_call_error
                for name in toolkit.get_async_functions():
                    properties = schemas[name]["properties"]
                    if name == "native_step":
                        assert properties["background_after"]["type"] == "integer"
                    else:
                        assert "background_after" not in properties
                assert (plugin / "executions").read_text().splitlines() == [step.result, "status"]
                assert await runtime.list_jobs(owner=owner, depth=0) == []
                release_background_tool_jobs(paths, instance)
                instance = pin_background_tool_jobs(config, paths)
                register_background_runtime(paths, runtime)
                rejected, _ = await _invoke(actor, "native_step", background_after=3)
                assert rejected.tool_call_error
                assert "exclude_toolkits" in str(rejected.result)
                assert await runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        release_background_tool_jobs(paths, instance)
        await runtime.shutdown()
