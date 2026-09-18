"""Skill access remains available through authorized managed SDK execution."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import AUTHORITY_METADATA_KEY, authority_snapshot
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.skills import build_agent_skills
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_skills import _write_skill, _write_skill_script
from tests.test_subagent_runtime import _config, _delivery_coordinator, _job

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("wait_timeout", [None, 0])
async def test_skill_access_through_managed_sdk_calls(
    tmp_path: Path, workspace: bool, wait_timeout: int | None
) -> None:
    """Configured and workspace skills retain instructions, references, and script policy."""
    config = _config(tmp_path)
    config.agents["lead"].skills = [] if workspace else ["demo"]
    coordinator = _delivery_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    root = tmp_path / ("workspace-skills" if workspace else "configured-skills")
    skill_path = _write_skill(root, "demo", "Sample skill")
    references = skill_path.parent / "references"
    references.mkdir()
    (references / "guide.md").write_text("Reference evidence")
    _write_skill_script(skill_path.parent, "hello.sh", "#!/bin/sh\nprintf 'Script evidence'\n")
    skills = build_agent_skills(
        "lead",
        config,
        paths,
        skill_roots=[tmp_path / "configured-skills"],
        workspace_skills_root=tmp_path / "workspace-skills",
        credential_keys=set(),
        output_file_policy=ToolOutputFilePolicy(tmp_path),
    )
    assert skills is not None
    calls = [
        _call("get_skill_instructions", "instructions", skill_name="demo", wait_timeout=wait_timeout),
        _call(
            "get_skill_reference", "reference", skill_name="demo", reference_path="guide.md", wait_timeout=wait_timeout
        ),
        _call("get_skill_script", "script", skill_name="demo", script_path="hello.sh", wait_timeout=wait_timeout),
        _call(
            "get_skill_script",
            "execute",
            skill_name="demo",
            script_path="hello.sh",
            execute=True,
            wait_timeout=wait_timeout,
        ),
    ]
    model = DelegationModel(id="test", responses=[ModelResponse(tool_calls=calls), ModelResponse(content="done")])
    install_tool_job_execution(model)
    agent = Agent(
        id="lead",
        model=model,
        skills=skills,
        metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "lead")},
    )
    owner = replace(_job().owner, transport_agent_name="lead")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(context):
                result = await agent.arun("Read the skill", session_id=owner.session_id)
                assert result.tools is not None
                assert all(not tool.tool_call_error for tool in result.tools)
                jobs = await coordinator.runtime.list_jobs(owner=owner, depth=0)
                assert len(jobs) == 4
                results = {}
                for job in jobs:
                    waited = await coordinator.runtime.wait(job.job_id, owner=owner, depth=0)
                    assert waited.job.status == "completed", waited.job.result
                    results[job.adapter["tool_call_id"]] = json.loads(waited.job.result)
                assert "# Body" in results["instructions"]["instructions"]
                assert results["reference"]["content"] == "Reference evidence"
                assert "Script evidence" in results["script"]["content"]
                if workspace:
                    assert "cannot be executed" in results["execute"]["error"]
                else:
                    assert results["execute"]["stdout"] == "Script evidence"
                if not workspace:
                    config.agents["lead"].skills = []
                    assert await coordinator.runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await coordinator.stop()
