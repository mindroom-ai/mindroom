"""The owned leader prompt, delegation tools, and team run reproduce the team prompt goldens.

Each golden is replayed twice: once through the production team builders with Agno, to capture the inputs MindRoom
hands the leader prompt and to check the owned rendering against Agno's, and once through the owned ``run_team``
with scripted models, to check every request the team sends against the golden's requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.prompt import render_system_message
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.team_runtime.delegate_tools import DELEGATE_TOOL_NAMES, delegate_functions
from mindroom.team_runtime.runtime import run_team
from mindroom.team_runtime.spec import TeamMember, TeamSpec
from tests.agno_parity.prompt_scenarios import SCENARIOS, load_golden, normalize
from tests.fakes.scripted_model import ScriptedModel, answer, calls
from tests.team_runtime.parity_support import (
    golden_toolkit,
    rendered_leader,
    request_dict,
    roster_members,
    run_team_scenario,
    wire_tool,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.runs import RunRecord

GOLDENS = ["team_coordinate", "team_collaborate"]
CLAUDE = ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic", provider_name="Anthropic")


def _is_leader_request(request: dict[str, Any]) -> bool:
    return any(tool["function"]["name"] in DELEGATE_TOOL_NAMES for tool in request["tools"])


async def _never(member: TeamMember, task: str) -> RunRecord:
    msg = f"No member runs while the tools are inspected: {member.spec.id} {task}"
    raise AssertionError(msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_the_leader_prompt_and_delegation_tool_equal_the_golden(name: str, tmp_path: Path) -> None:
    """The leader's system message renders byte for byte as Agno's and as the golden, and so does its tool."""
    golden = load_golden(name)
    leader_requests = [request for request in golden["requests"] if _is_leader_request(request)]

    run = await run_team_scenario(name, tmp_path)

    assert SCENARIOS[name].description == golden["description"]
    assert normalize(run.input, tmp_path) == golden["input"]
    # Agno built the leader's system message once per run and sent it with both leader requests.
    [capture] = run.leader_prompts
    assert len(leader_requests) == 2
    assert rendered_leader(capture) == capture.agno_content
    assert {request["messages"][0]["content"] for request in leader_requests} == {
        normalize(rendered_leader(capture), tmp_path),
    }

    toolkit = delegate_functions(
        roster_members(capture),
        capture.mode,
        record=TeamRunRecord(run_id="r"),
        run_member=_never,
    )
    [function] = toolkit.functions
    assert [wire_tool(function.definition())] == leader_requests[0]["tools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_the_owned_team_run_sends_every_golden_request(name: str, tmp_path: Path) -> None:
    """Members get their prompt and the leader's task alone; the leader gets the roster prompt and the member results."""
    golden = load_golden(name)
    member_requests = [request for request in golden["requests"] if not _is_leader_request(request)]
    leader_requests = [request for request in golden["requests"] if _is_leader_request(request)]
    run = await run_team_scenario(name, tmp_path)
    capture = run.leader_prompts[0]

    members: list[TeamMember] = []
    member_models: list[ScriptedModel] = []
    for roster in capture.members:
        [prompt] = [prompt for prompt in run.member_prompts if prompt.spec.role == roster.role]
        system = render_system_message(prompt.spec)
        [request] = [r for r in member_requests if normalize(system.content, tmp_path) == r["messages"][0]["content"]]
        model = ScriptedModel([answer("ok")], identity=CLAUDE)
        member_models.append(model)
        spec = AgentSpec(
            id=roster.id,
            name=roster.name,
            model=model,
            system_message=system,
            toolkits=(golden_toolkit(request["tools"]),),
        )
        members.append(TeamMember(spec=spec, role=roster.role))
    [delegating] = [message for message in leader_requests[1]["messages"] if message["role"] == "assistant"]
    leader_model = ScriptedModel([calls(*delegating["tool_calls"]), answer("ok")], identity=CLAUDE)
    team = TeamSpec(
        id="launch",
        name="Launch Team",
        mode=capture.mode,
        model=leader_model,
        system_message=Message(role="system", content=rendered_leader(capture, members)),
        members=tuple(members),
    )
    run_input = [Message.from_dict(message) for message in leader_requests[0]["messages"][1:]]

    events = [
        event
        async for event in run_team(team, run_input, ctx=RunContext(run_id="run-launch", session_id="session-launch"))
    ]

    finished = events[-1]
    assert isinstance(finished, RunFinished)
    assert (finished.record.status, finished.record.content) == (RunStatus.completed, "ok")
    sent = [
        request_dict(model.identity, request) for model in [*member_models, leader_model] for request in model.requests
    ]
    assert [normalize(request, tmp_path) for request in sent] == golden["requests"]
