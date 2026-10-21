"""Run the team prompt-golden scenarios with Agno and capture what MindRoom handed the team's prompt builder.

While Agno still runs ``teams.team_response_stream``, ``TeamPromptCapture`` reads the inputs MindRoom gave Agno's
team system-message builder, so the owned leader prompt is compared with the goldens through the production
builders, as ``tests/agent_loop/prompt_parity_support.py`` does for agent prompts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agno.team import Team
from agno.team import _messages as agno_team_messages
from agno.team.mode import TeamMode as AgnoTeamMode
from agno.utils.team import get_member_id

from mindroom.agent_loop.prompt import model_note_for
from mindroom.agent_loop.spec import AgentSpec
from mindroom.team_runtime.prompt import render_leader_system_message
from mindroom.team_runtime.spec import TeamMember, TeamMode
from mindroom.tool_system.agno_toolkits import AdaptedToolkit
from mindroom.tool_system.tool_function import ToolFunction
from tests.agent_loop.prompt_parity_support import CapturedPrompt, PromptCapture, agno_model_capabilities
from tests.agno_parity.prompt_scenarios import SCENARIOS, ModelCall, Recorder, message_dict, response_format_dict
from tests.fakes.scripted_model import ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from agno.models.message import Message as AgnoMessage
    from agno.run import RunContext as AgnoRunContext
    from agno.session.team import TeamSession

    from mindroom.llm.provider import ModelIdentity, ModelRequest, ToolSpec


@dataclass(frozen=True)
class CapturedMember:
    """One roster entry as MindRoom handed it to Agno: the member's id, display name, and role text."""

    id: str
    name: str
    role: str


@dataclass(frozen=True)
class CapturedLeaderPrompt:
    """One team system message Agno built: the owned prompt inputs MindRoom handed it, and what Agno rendered."""

    members: tuple[CapturedMember, ...]
    mode: TeamMode
    tool_instructions: tuple[str, ...]
    model_note: str | None
    additional_context: str | None
    agno_content: str


def _check_modeled_inputs(
    team: Team,
    tools: list[Any] | None,
    run_context: AgnoRunContext | None,
    *,
    add_session_state_to_context: bool | None,
    media: Sequence[object | None],
) -> None:
    """Fail when a team uses a system-message input of Agno's that the owned leader prompt does not model."""
    assert team.system_message is None
    assert (team.description, team.role, team.instructions) == (None, None, None)
    assert team.model is not None
    assert team.model.get_instructions_for_model(tools) is None
    assert team.model.system_prompt is None
    assert not team.markdown
    assert not team.add_datetime_to_context
    assert not team.add_location_to_context
    assert not team.add_name_to_context
    assert team._result_store is None
    assert team.expected_output is None
    assert team.skills is None
    assert team._learning is None
    assert team.knowledge is None
    assert not team.add_memories_to_context
    assert not team.add_session_summary_to_context
    assert not team.add_member_tools_to_context
    assert not team.get_member_information_tool
    assert team.determine_input_for_members
    assert not team.add_team_history_to_members
    assert not team.share_member_interactions
    assert team.mode in (AgnoTeamMode.coordinate, AgnoTeamMode.broadcast)
    for member in team.members:
        assert not isinstance(member, Team)
        assert member.description is None
        assert member.role is not None
    assert not add_session_state_to_context
    assert all(item is None for item in media)
    assert run_context is None or run_context.output_schema is None


@dataclass
class TeamPromptCapture:
    """Record the owned prompt inputs of every team system message Agno builds while installed."""

    prompts: list[CapturedLeaderPrompt] = field(default_factory=list)

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Wrap Agno's team system-message builder, which every leader request calls."""
        build = agno_team_messages.aget_system_message

        async def capture(
            team: Team,
            session: TeamSession,
            run_context: AgnoRunContext | None = None,
            audio: Sequence[object] | None = None,
            images: Sequence[object] | None = None,
            videos: Sequence[object] | None = None,
            files: Sequence[object] | None = None,
            tools: list[Any] | None = None,
            add_session_state_to_context: bool | None = None,
            input: object = None,  # noqa: A002 - Agno's parameter name
        ) -> AgnoMessage | None:
            media = (audio, images, videos, files)
            _check_modeled_inputs(
                team,
                tools,
                run_context,
                add_session_state_to_context=add_session_state_to_context,
                media=media,
            )
            message = await build(
                team,
                session=session,
                run_context=run_context,
                audio=audio,
                images=images,
                videos=videos,
                files=files,
                tools=tools,
                add_session_state_to_context=add_session_state_to_context,
                input=input,
            )
            assert message is not None
            assert isinstance(message.content, str)
            assert team.model is not None
            self.prompts.append(
                CapturedLeaderPrompt(
                    members=tuple(
                        CapturedMember(id=get_member_id(member) or "", name=member.name or "", role=member.role or "")
                        for member in team.members
                    ),
                    mode=TeamMode.COLLABORATE if team.mode is AgnoTeamMode.broadcast else TeamMode.COORDINATE,
                    tool_instructions=tuple(team._tool_instructions or ()),
                    model_note=model_note_for(agno_model_capabilities(team.model), offers_tools=bool(tools)),
                    additional_context=team.additional_context,
                    agno_content=message.content,
                ),
            )
            return message

        with patch.object(agno_team_messages, "aget_system_message", capture):
            yield


@dataclass(frozen=True)
class TeamScenarioRun:
    """What one team scenario sent: its recorded input, model calls, member prompt captures, and leader captures."""

    input: dict[str, Any]
    calls: list[ModelCall]
    member_prompts: list[CapturedPrompt]
    leader_prompts: list[CapturedLeaderPrompt]


async def run_team_scenario(name: str, root: Path) -> TeamScenarioRun:
    """Run one team scenario under ``root`` against the recording model, capturing member and leader prompts."""
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)
    members = PromptCapture()
    leaders = TeamPromptCapture()
    with recorder.installed(), members.installed(), leaders.installed():
        output = await scenario.run(root)
    return TeamScenarioRun(output.input, recorder.ordered_calls(), members.prompts, leaders.prompts)


def roster_members(capture: CapturedLeaderPrompt) -> tuple[TeamMember, ...]:
    """Return the captured roster as team members whose specs carry only what the leader prompt reads."""
    return tuple(
        TeamMember(
            spec=AgentSpec(id=member.id, name=member.name, model=ScriptedModel([]), system_message=None),
            role=member.role,
        )
        for member in capture.members
    )


def rendered_leader(capture: CapturedLeaderPrompt, members: Sequence[TeamMember] | None = None) -> str:
    """Return the owned leader system message for one capture."""
    content = render_leader_system_message(
        roster_members(capture) if members is None else members,
        capture.mode,
        tool_instructions=capture.tool_instructions,
        model_note=capture.model_note,
        additional_context=capture.additional_context,
    ).content
    assert isinstance(content, str)
    return content


def wire_tool(spec: ToolSpec) -> dict[str, Any]:
    """Return the request tool entry the prompt goldens record: Agno's ``Function.to_dict()`` in a function wrapper."""
    fields = {
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.parameters),
        "strict": spec.strict,
        "requires_confirmation": spec.requires_confirmation,
        "external_execution": spec.external_execution,
        "approval_type": spec.approval_type,
    }
    return {"type": "function", "function": {key: value for key, value in fields.items() if value is not None}}


def _ok(**_arguments: object) -> str:
    return "ok"


def golden_toolkit(tools: Sequence[dict[str, Any]]) -> AdaptedToolkit:
    """Return a toolkit offering exactly the tool definitions a golden request recorded, answering ``ok``."""
    functions = tuple(
        ToolFunction(
            name=tool["function"]["name"],
            description=tool["function"].get("description"),
            parameters=tool["function"]["parameters"],
            strict=tool["function"].get("strict"),
            requires_confirmation=tool["function"].get("requires_confirmation"),
            external_execution=tool["function"].get("external_execution"),
            approval_type=tool["function"].get("approval_type"),
            entrypoint=_ok,
        )
        for tool in tools
    )
    return AdaptedToolkit(name="golden", functions=functions, instructions=None)


def request_dict(identity: ModelIdentity, request: ModelRequest) -> dict[str, Any]:
    """Return one owned request as the prompt goldens record a request."""
    return {
        "model": {"id": identity.id, "provider": identity.provider},
        "stream": request.stream,
        "messages": [message_dict(message) for message in request.messages],
        "tools": [wire_tool(tool) for tool in request.tools],
        "tool_choice": request.tool_choice,
        "response_format": response_format_dict(request.response_format),
        "deferred_tool_names": sorted(request.deferred_tool_names),
    }
