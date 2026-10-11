"""Workspace output policies cover managed result retrieval."""

from __future__ import annotations

from ast import literal_eval
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.delegation.background import delegation_outcome
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, read_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import (
    lookup,
    start_delegation_job,
    start_job,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.run.agent import RunOutput

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_LARGE_RESULT = "Full output: café 🦀\n" * 10_000
pytestmark = pytest.mark.asyncio


@dataclass
class _OutputAgent:
    agent: Agent
    model: DelegationModel
    runtime: ToolJobRuntime
    owner: ToolExecutionIdentity
    workspace: Path
    paths: RuntimePaths
    config: Config

    async def call(self, name: str, **arguments: object) -> RunOutput:
        self.model.responses = [
            ModelResponse(tool_calls=[_call(name, "output-call", **arguments)]),
            ModelResponse(content="done"),
        ]
        return await self.agent.arun("retrieve output", session_id=self.owner.session_id)

    async def save_job(self) -> str:
        async def operation() -> BackgroundOutcome:
            payload = encode_result_payload(ToolResultPayload(_LARGE_RESULT))
            return BackgroundOutcome("completed", _LARGE_RESULT, result_payload=payload)

        job_id = "a" * 64
        await start_job(
            self.runtime,
            job_id,
            tool_name="saved_output",
            depth=0,
            adapter={},
            owner=self.owner,
            operation=operation,
        )
        ready = await self.runtime.wait(job_id, owner=self.owner, depth=0)
        await self.runtime.release_wait(job_id, ready.claim)
        return job_id


@pytest_asyncio.fixture
async def output_agent(tmp_path: Path) -> AsyncIterator[_OutputAgent]:
    """Build a workspace agent with real SDK dispatch and isolated durable jobs."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])},
        models={"default": {"provider": "openai", "id": "gpt-6-astra"}},
        memory={"backend": "file"},
        defaults={"tools": []},
    )
    bind_runtime_paths(config, runtime_paths=paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    agent = create_agent("leader", config, paths, execution_identity=owner, persist_runtime_state=False)
    model = DelegationModel(id="output-test")
    install_tool_job_execution(model)
    agent.model = model
    workspace = resolve_agent_runtime("leader", config, paths, execution_identity=owner).workspace
    assert workspace is not None
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                yield _OutputAgent(agent, model, runtime, owner, workspace.root, paths, config)
    finally:
        await runtime.shutdown()
        if agent.db is not None:
            agent.db.close()


@pytest.mark.parametrize("explicit", [False, True])
async def test_job_retrieval_applies_workspace_output_policy(output_agent: _OutputAgent, explicit: bool) -> None:
    """Completed values redirect intact to the requested or automatic workspace file."""
    case = output_agent
    job_id = await case.save_job()
    arguments = {"mindroom_output_path": "results/job.txt"} if explicit else {}
    response = await case.call("job", action="wait", job_id=job_id, **arguments)
    assert response.tools
    assert not response.tools[0].tool_call_error
    result = literal_eval(response.tools[0].result)
    receipt = result["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved = (case.workspace / receipt["path"]).read_bytes()
    assert saved == _LARGE_RESULT.encode()
    assert "truncated" not in saved.decode()
    assert len(response.tools[0].result) < 12_000
    if explicit:
        assert receipt["path"] == "results/job.txt"
    else:
        assert receipt["auto_saved"] is True
    saved_job = await lookup(case.runtime, job_id, owner=case.owner, depth=0)
    assert (await read_result_payload(case.runtime, saved_job)).value == _LARGE_RESULT


async def test_job_output_path_is_validated_before_claiming_result(output_agent: _OutputAgent) -> None:
    """A bad destination neither escapes the workspace nor consumes the stored value."""
    case = output_agent
    job_id = await case.save_job()
    response = await case.call("job", action="wait", job_id=job_id, mindroom_output_path="../escape.txt")
    assert response.tools
    assert not response.tools[0].tool_call_error
    assert literal_eval(response.tools[0].result)["mindroom_tool_output"]["status"] == "error"
    saved = await lookup(case.runtime, job_id, owner=case.owner, depth=0)
    assert not saved.consumed
    assert not (case.workspace.parent / "escape.txt").exists()


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_delegation_job_wait_redirects_saved_result(output_agent: _OutputAgent, explicit: bool) -> None:
    """Retrieving a subagent's saved result applies the retrieval output policy."""
    case = output_agent
    child = prepare_child_turn(
        "leader",
        "leader",
        "saved output",
        owner=case.owner,
        config=case.config,
        runtime_paths=case.paths,
        depth=0,
    )

    async def completed() -> BackgroundOutcome:
        return delegation_outcome("completed", _LARGE_RESULT)

    job = await start_delegation_job(case.runtime, child, owner=case.owner, operation=completed)
    ready = await case.runtime.wait(job.job_id, owner=case.owner, depth=0)
    await case.runtime.release_wait(job.job_id, ready.claim)
    case.agent.db = create_session_storage("leader", case.config, case.paths, case.owner)
    arguments = {"mindroom_output_path": "results/child.txt"} if explicit else {}
    response = await case.call("job", action="wait", job_id=job.job_id, **arguments)
    assert response.status == RunStatus.completed
    assert response.tools
    assert not response.tools[0].tool_call_error
    receipt = literal_eval(response.tools[0].result)["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (case.workspace / receipt["path"]).read_bytes() == _LARGE_RESULT.encode()
