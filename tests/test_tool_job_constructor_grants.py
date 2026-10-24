"""Retained functions lose execution and result access when authored constructor settings change."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

import pytest

from mindroom.config.models import ToolConfigEntry
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError
from mindroom.tool_system.construction import tool_config_signature
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    job_actor,
    lookup,
    managed_team_config,
    pending_outcome,
    run_job_turn,
    team_coordinator,
    toolkit_job_tools,
)

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
    """An accepted outer job cannot enter a now-disabled real file or shell function.

    The refused nested call fails as that call, so the outer work reports it.
    """
    config = managed_team_config(tmp_path)
    config.defaults.tools = []
    entry = ToolConfigEntry(name=toolkit_name, defer=deferred, overrides={flag: True})
    config.agents["lead"].tools = [entry]
    coordinator = team_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(completed_delegation_job().owner, transport_agent_name="lead")
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
    tools = toolkit_job_tools(toolkit, authored_name=toolkit_name, snapshot=authority_snapshot(config, "lead"))
    tool = tools[function_name]
    model = ScriptedModel([calls(tool_call(function_name, "nested-call", **arguments)), answer("done")])
    actor = job_actor(*tools.values(), name="lead")
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        started.set()
        await release.wait()
        try:
            record = await run_job_turn(actor, model, "Write the marker", session_id=owner.session_id or "session")
            [nested] = record.tools or []
            return BackgroundOutcome("failed" if nested.is_error else "completed", str(nested.result))
        finally:
            finished.set()

    try:
        await coordinator.sync()
        runtime = coordinator.runtime
        async with execution_resources():
            with tool_runtime_context(context):
                job, claim = await runtime.start(
                    "outer-job",
                    tool_name=function_name,
                    depth=0,
                    toolkit_name=toolkit_name,
                    adapter={"origin": function_provenance(tool), "authority": dict(tool.authority)},
                    owner=owner,
                    operation=operation,
                )
                await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
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
                await asyncio.wait_for(finished.wait(), JOB_TEST_TIMEOUT)
                if revoked:
                    assert not (tmp_path / "nested.txt").exists()
                    assert await runtime.list_jobs(owner=owner, depth=0) == []
                    assert pending_outcome(runtime, job.job_id) is None
                    with pytest.raises(JobAccessError):
                        await lookup(runtime, job.job_id, owner=owner, depth=0)
                    entry.overrides[flag] = True
                else:
                    assert (tmp_path / "nested.txt").read_text() == "accepted"
                waited = await runtime.wait(job.job_id, owner=owner, depth=0, claim=claim)
                if revoked:
                    assert waited.job.status == "failed"
                    assert "no longer authorized" in (waited.job.result or "")
                await runtime.release_wait(job.job_id, waited.claim)
    finally:
        release.set()
        await coordinator.stop()


def test_constructor_signature_accepts_every_yaml_override_value() -> None:
    """An unquoted YAML date must not crash toolkit construction, and still distinguishes settings."""
    first = tool_config_signature({"start": date(2026, 9, 28), "include_tools": ["ignored"]})
    assert first == tool_config_signature({"start": date(2026, 9, 28)})
    assert first != tool_config_signature({"start": date(2026, 9, 29)})
