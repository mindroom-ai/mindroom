"""Workspace output policies cover managed result retrieval and generated knowledge tools."""

from __future__ import annotations

import json
from ast import literal_eval
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from agno.knowledge.knowledge import Knowledge
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.delegation.background import start_delegation
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import _call
from tests.test_tool_job_exclusions import _SchemaRecordingModel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.run.agent import RunOutput

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_LARGE_RESULT = "Full output: café 🦀\n" * 10_000
pytestmark = pytest.mark.asyncio


@dataclass
class _OutputAgent:
    agent: Agent
    model: _SchemaRecordingModel
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
            return BackgroundOutcome("completed", _LARGE_RESULT)

        job_id = "a" * 64
        await self.runtime.start(JobSpec(job_id, "saved_output", 0), owner=self.owner, operation=operation)
        ready = await self.runtime.wait(job_id, owner=self.owner, depth=0)
        await self.runtime.release_wait(job_id, ready.token)
        return job_id


@pytest.fixture
def managed() -> bool:
    """Most cases exercise managed jobs; knowledge also covers the disabled path."""
    return True


@pytest_asyncio.fixture
async def output_agent(tmp_path: Path, managed: bool) -> AsyncIterator[_OutputAgent]:
    """Build a workspace agent with real SDK dispatch and isolated durable jobs."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=managed),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"], knowledge_bases=["probe"])},
        knowledge_bases={"probe": {"path": str(tmp_path / "knowledge")}},
        models={"default": {"provider": "openai", "id": "gpt-6-astra"}},
        memory={"backend": "file"},
        defaults={"tools": []},
    )
    bind_runtime_paths(config, runtime_paths=paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    if managed:
        register_background_runtime(paths, runtime)
    agent = create_agent(
        "leader",
        config,
        paths,
        execution_identity=owner,
        persist_runtime_state=False,
        knowledge=Knowledge(name="probe"),
    )
    model = _SchemaRecordingModel(id="output-test")
    if managed:
        install_tool_job_execution(model)
    agent.model = model
    workspace = resolve_agent_runtime("leader", config, paths, execution_identity=owner).workspace
    assert workspace is not None
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                yield _OutputAgent(agent, model, runtime, owner, workspace.root, paths, config)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()
        if agent.db is not None:
            agent.db.close()


@pytest.mark.parametrize("action", ["wait", "inspect"])
@pytest.mark.parametrize("explicit", [False, True])
async def test_job_retrieval_applies_workspace_output_policy(
    output_agent: _OutputAgent,
    action: str,
    explicit: bool,
) -> None:
    """Completed values redirect intact; inspection continues to describe the bounded summary."""
    case = output_agent
    job_id = await case.save_job()
    arguments = {"mindroom_output_path": "results/job.txt"} if explicit else {}
    response = await case.call("job", action=action, job_id=job_id, **arguments)
    assert response.tools
    assert not response.tools[0].tool_call_error
    if action == "inspect" and not explicit:
        result = json.loads(response.tools[0].result)
        assert result["summary"] == _LARGE_RESULT[:500]
        assert result["summary_truncated"] is True
        return
    result = literal_eval(response.tools[0].result)
    receipt = result["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved = (case.workspace / receipt["path"]).read_bytes()
    if action == "wait":
        assert saved == _LARGE_RESULT.encode()
        assert "truncated" not in saved.decode()
    else:
        assert json.loads(saved)["summary"] == _LARGE_RESULT[:500]
        assert json.loads(saved)["summary_truncated"] is True
    assert len(response.tools[0].result) < 12_000
    if explicit:
        assert receipt["path"] == "results/job.txt"
    else:
        assert receipt["auto_saved"] is True
    assert (await case.runtime.lookup(job_id, owner=case.owner, depth=0)).result == _LARGE_RESULT


async def test_job_output_path_is_validated_before_claiming_result(output_agent: _OutputAgent) -> None:
    """A bad destination neither escapes the workspace nor consumes the stored value."""
    case = output_agent
    job_id = await case.save_job()
    response = await case.call("job", action="wait", job_id=job_id, mindroom_output_path="../escape.txt")
    assert response.tools
    assert not response.tools[0].tool_call_error
    assert literal_eval(response.tools[0].result)["mindroom_tool_output"]["status"] == "error"
    saved = await case.runtime.lookup(job_id, owner=case.owner, depth=0)
    assert not saved.wait_acknowledged
    assert not (case.workspace.parent / "escape.txt").exists()


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_generated_knowledge_search_uses_shared_output_policy(
    output_agent: _OutputAgent,
    managed: bool,
    explicit: bool,
) -> None:
    """Knowledge results use the same full-file contract before managed result encoding."""
    case = output_agent

    async def retrieve(query: str, num_documents: int | None = None) -> list[dict[str, str]]:
        del query, num_documents
        return [{"content": _LARGE_RESULT, "name": "synthetic document"}]

    case.agent.knowledge_retriever = retrieve
    arguments: dict[str, object] = {"query": "full output"}
    if explicit:
        arguments["mindroom_output_path"] = "results/knowledge.json"
    if managed:
        arguments["wait_timeout"] = 0
    response = await case.call("search_knowledge_base", **arguments)
    assert response.tools
    assert not response.tools[0].tool_call_error
    assert "mindroom_output_path" in case.model.schemas["search_knowledge_base"]["properties"]
    if managed:
        handle = json.loads(response.tools[0].result)
        response = await case.call("job", action="wait", job_id=handle["job_id"])
        assert response.tools
        assert not response.tools[0].tool_call_error
    receipt = literal_eval(response.tools[0].result)["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved = (case.workspace / receipt["path"]).read_bytes()
    assert json.loads(saved)[0]["content"].encode() == _LARGE_RESULT.encode()
    assert "truncated" not in saved.decode()
    if explicit:
        assert receipt["path"] == "results/knowledge.json"
    else:
        assert receipt["auto_saved"] is True


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_delegation_job_wait_redirects_saved_result(output_agent: _OutputAgent, explicit: bool) -> None:
    """External native waits retain their identity and apply the retrieval output policy."""
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
        return BackgroundOutcome("completed", _LARGE_RESULT)

    async def never_run(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "Retrieval must not execute the child again"
        raise AssertionError(msg)

    job = await start_delegation(case.runtime, child, owner=case.owner, operation=completed)
    ready = await case.runtime.wait(job.job_id, owner=case.owner, depth=0)
    await case.runtime.release_wait(job.job_id, ready.token)
    case.agent.db = create_session_storage("leader", case.config, case.paths, case.owner)
    arguments = {"mindroom_output_path": "results/child.txt"} if explicit else {}
    paused = await case.call("job", action="wait", job_id=job.job_id, **arguments)
    assert paused.status == RunStatus.paused
    response = await drive_delegations(
        case.agent,
        paused,
        run_child=never_run,
        agent_name="leader",
        config=case.config,
        runtime_paths=case.paths,
        execution_identity=case.owner,
    )
    assert response.tools
    assert not response.tools[0].tool_call_error
    receipt = json.loads(response.tools[0].result)["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (case.workspace / receipt["path"]).read_bytes() == _LARGE_RESULT.encode()
