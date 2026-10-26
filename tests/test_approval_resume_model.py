"""A saved approval's resume runs its tool calls on the model its run uses.

A saved approval may name no runtime model, and then its run uses the entity's configured model. The resumed calls
carry that model, so a tool that follows the reply's model, such as ``claude_agent``, sees the one the run uses.
Each test pauses a real reply on the gated ``write_report`` and resumes it through the production approval
execution under a tool context that names no model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    ReportTools,
    ScriptedAgents,
    _runtime_paths,
    approval_calls_for,
    approval_executor,
    registered_report_tool,
    respond,
    respond_team,
    resume,
    resume_team,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

REQUESTER = "@alice:example.org"
ROOM = "!room:example.org"
THREAD = "$thread:example.org"


def _config(tmp_path: Path) -> Config:
    """Return ``code`` on the ``writer`` model, alone and as the only member of ``squad`` on the ``lead`` model."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            agents={"code": AgentConfig(display_name="Code", model="writer", tools=["report"])},
            teams={
                "squad": TeamConfig(
                    display_name="Squad",
                    role="Writes reports.",
                    agents=["code"],
                    mode="coordinate",
                    model="lead",
                ),
            },
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "writer": ModelConfig(provider="openai", id="writer-model"),
                "lead": ModelConfig(provider="openai", id="lead-model"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"rules": [{"match": "write_report", "action": "require_approval"}]},
        ),
        "code",
        users=[REQUESTER],
    )
    entity_ids(config, paths)
    return config


@pytest.fixture
def seen_models(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str | None]]:
    """Record, for each ``write_report`` run, the tool context's model and the model its call carries."""
    seen: list[tuple[str | None, str | None]] = []

    async def write_report(self: ReportTools) -> str:
        del self
        context = get_tool_runtime_context()
        call = current_tool_call()
        seen.append(
            (
                None if context is None else context.active_model_name,
                None if call is None else call.context.model_name,
            ),
        )
        return "report written"

    monkeypatch.setattr(ReportTools, "write_report", write_report)
    return seen


async def test_an_agent_approval_resume_runs_its_tools_on_the_agent_s_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen_models: list[tuple[str | None, str | None]],
) -> None:
    """A resume saved without a runtime model runs on the agent's configured model, and so do its tool calls."""
    config = _config(tmp_path)
    paths = _runtime_paths(tmp_path)
    identity = ToolExecutionIdentity("matrix", "code", REQUESTER, ROOM, THREAD, THREAD, "session")
    code = ScriptedModel([calls(tool_call("write_report", "call-1")), answer("Report written.")])
    ScriptedAgents({"code": code}).install(monkeypatch)
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    with registered_report_tool():
        paused = await respond("code", "Write the report", **options)
        assert isinstance(paused, PausedAttempt)
        approvals = approval_calls_for(paused, default_agent="code", toolkit_names={"write_report": "report"})
        result = await resume(
            approval_executor(config, paths),
            # The approval names no runtime model, so the resume runs on the agent's configured one.
            replace(paused, runtime_model_name=None),
            approvals,
            agent_name="code",
            **options,
            decisions={call.tool_call_id: True for call in approvals},
        )

    assert isinstance(result, CompletedApprovalRun)
    assert seen_models == [("writer", "writer")]


async def test_a_team_approval_resume_carries_the_team_s_model_on_its_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seen_models: list[tuple[str | None, str | None]],
) -> None:
    """A team's resumed member call carries the team's model, as a live turn's does, and shows the member its own."""
    config = _config(tmp_path)
    paths = _runtime_paths(tmp_path)
    identity = ToolExecutionIdentity("matrix", "squad", REQUESTER, ROOM, THREAD, THREAD, "session")
    code = ScriptedModel([calls(tool_call("write_report", "call-1")), answer("Report written.")])
    leader = ScriptedModel(
        [calls(tool_call("delegate_task_to_member", "member", member_id="code", task="Write it")), answer("Done.")],
    )
    ScriptedAgents({"code": code}, team=leader).install(monkeypatch)
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    with registered_report_tool():
        paused = await respond_team("squad", "Write the report", **options)
        assert isinstance(paused, PausedAttempt)
        assert paused.runtime_model_name == "lead"
        approvals = approval_calls_for(paused, default_agent="code", toolkit_names={"write_report": "report"})
        result = await resume_team(
            "squad",
            paused,
            approvals,
            **options,
            decisions={call.tool_call_id: True for call in approvals},
        )

    assert isinstance(result, CompletedApprovalRun)
    assert seen_models == [("writer", "lead")]
