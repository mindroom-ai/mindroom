"""Retained functions lose execution and result access when authored constructor settings change."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.config.models import ToolConfigEntry
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import (
    authority_snapshot,
    bind_actor_authority,
    bind_toolkit_authority,
    function_authority,
)
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError, JobSpec
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_subagent_runtime import _config, _delivery_coordinator, _job

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    ("toolkit_name", "function_name", "flag", "arguments"),
    [
        ("file", "save_file", "enable_save_file", {"file_name": "nested.txt", "contents": "accepted"}),
        (
            "shell",
            "run_shell_command",
            "enable_run_shell_command",
            {"args": ["sh", "-c", "printf accepted > nested.txt"]},
        ),
    ],
)
async def test_retained_tool_constructor_grants_gate_nested_execution_and_results(  # noqa: PLR0915
    tmp_path: Path,
    toolkit_name: str,
    function_name: str,
    flag: str,
    arguments: dict[str, object],
    deferred: bool,
    revoked: bool,
) -> None:
    """An accepted outer job cannot enter a now-disabled real file or shell function."""
    config = _config(tmp_path)
    config.defaults.tools = []
    entry = ToolConfigEntry(name=toolkit_name, defer=deferred, overrides={flag: True})
    config.agents["lead"].tools = [entry]
    coordinator = _delivery_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(_job().owner, transport_agent_name="lead")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="lead",
        transport_agent_name="lead",
    )
    toolkit = get_tool_by_name(
        toolkit_name,
        paths,
        tool_config_overrides=entry.overrides,
        tool_init_overrides={"base_dir": str(tmp_path)},
        worker_target=None,
        disable_sandbox_proxy=True,
    )
    bind_toolkit_authority(toolkit, authored_name=toolkit_name)
    function = toolkit.get_async_functions()[function_name]
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call(function_name, "nested-call", **arguments)]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    actor = bind_actor_authority(
        Agent(
            id="lead",
            model=model,
            tools=[toolkit],
        ),
        authority_snapshot(config, "lead"),
    )
    function._agent = actor
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    wait_claim = uuid4().hex

    async def operation() -> BackgroundOutcome:
        started.set()
        await release.wait()
        try:
            response = await actor.arun("Write the marker", session_id=owner.session_id)
            return BackgroundOutcome(
                "failed" if response.status == RunStatus.error else "completed",
                str(response.content),
            )
        finally:
            finished.set()

    try:
        await coordinator.sync()
        runtime = coordinator.runtime
        async with execution_resources():
            with tool_runtime_context(context):
                job = await runtime.start(
                    JobSpec(
                        "outer-job",
                        function_name,
                        0,
                        toolkit_name=toolkit_name,
                        adapter={"origin": function_provenance(function), "authority": function_authority(function)},
                    ),
                    owner=owner,
                    operation=operation,
                    initial_wait_token=wait_claim,
                )
                await asyncio.wait_for(started.wait(), 2)
                assert len(await runtime.list_jobs(owner=owner, depth=0)) == 1
                if revoked:
                    entry.overrides[flag] = False
                    current = get_tool_by_name(
                        toolkit_name,
                        paths,
                        tool_config_overrides=entry.overrides,
                        worker_target=None,
                        disable_sandbox_proxy=True,
                    )
                    assert function_name not in current.get_async_functions()
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                if revoked:
                    assert not (tmp_path / "nested.txt").exists()
                    assert await runtime.list_jobs(owner=owner, depth=0) == []
                    assert await runtime.outcome(job.job_id, job.generation) is None
                    with pytest.raises(JobAccessError):
                        await runtime.lookup(job.job_id, owner=owner, depth=0)
                    entry.overrides[flag] = True
                else:
                    assert (tmp_path / "nested.txt").read_text() == "accepted"
                waited = await runtime.wait(job.job_id, owner=owner, depth=0, reserved_token=wait_claim)
                if revoked:
                    assert waited.job.status == "failed"
                    assert "no longer authorized" in (waited.job.result or "")
                await runtime.release_wait(job.job_id, waited.token)
    finally:
        release.set()
        await coordinator.stop()
