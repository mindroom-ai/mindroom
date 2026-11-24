"""The production team turn reproduces the team prompt goldens.

Each golden is replayed through ``team_response_stream`` with the recording provider: the members get their prompt
and the leader's task alone, the leader gets the roster prompt and the delegation tool, then both member results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.prompt import MARKDOWN_OUTPUT_FORMAT, MATRIX_MARKDOWN_OUTPUT_FORMAT
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.llm.messages import Message, system_text
from mindroom.teams.members import build_team_spec, resolve_exact_team_members
from mindroom.teams.prompt import render_leader_system_messages
from mindroom.teams.spec import TeamMode
from tests.agno_parity.golden_files import render
from tests.agno_parity.prompt_scenarios import golden_path, load_golden, record_scenario, regenerated_golden
from tests.conftest import bind_runtime_paths, install_model_providers, test_runtime_paths, unstored_team_scope_context
from tests.fakes.scripted_model import ScriptedModel
from tests.teams.squad import squad

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = ["team_coordinate", "team_collaborate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_the_production_team_turn_sends_every_golden_request(name: str, tmp_path: Path) -> None:
    """Every request of the team turn equals the golden's byte for byte, through the owned provider layer."""
    run = await record_scenario(name, tmp_path)

    regenerated = regenerated_golden(name, run, tmp_path)
    assert regenerated == load_golden(name)
    assert render(regenerated) == golden_path(name).read_text(encoding="utf-8")
    assert run.calls
    assert all(isinstance(message, Message) for call in run.calls for message in call.messages)
    # The leader's prompt is built once per turn and sent with both of its requests.
    leader_requests = [
        call for call in run.calls if any(tool.name.startswith("delegate_task_to_") for tool in call.tools)
    ]
    assert len(leader_requests) == 2
    assert len({request.messages[0].content for request in leader_requests}) == 1


def test_the_leader_prompt_keeps_its_roster_stable_and_its_date_in_the_turn_part() -> None:
    """The roster and delegation rules are the stable system message; a new date changes only the turn message."""
    members = squad(ScriptedModel([]), ScriptedModel([]), ScriptedModel([])).members

    def render(day: str) -> tuple[Message, ...]:
        return render_leader_system_messages(
            members,
            TeamMode.COORDINATE,
            identity="You are Squad.",
            markdown=False,
            tool_instructions=(),
            turn_context=day,
        )

    monday, tuesday = render("Monday."), render("Tuesday.")

    assert monday[0].content == tuesday[0].content
    assert str(monday[0].content).startswith("<your_role>\nYou are Squad.\n</your_role>\n\n<team>\n")
    assert str(monday[0].content).endswith("</team>")
    assert (monday[1].content, tuesday[1].content) == ("Monday.", "Tuesday.")


@pytest.mark.parametrize(
    ("outside_matrix", "markdown", "expected", "absent"),
    [
        (False, True, MATRIX_MARKDOWN_OUTPUT_FORMAT, None),
        (True, True, MARKDOWN_OUTPUT_FORMAT, MATRIX_MARKDOWN_OUTPUT_FORMAT),
        (False, False, None, MARKDOWN_OUTPUT_FORMAT),
    ],
)
def test_team_leader_and_members_use_matrix_markdown_format_only_inside_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outside_matrix: bool,
    markdown: bool,
    expected: str | None,
    absent: str | None,
) -> None:
    """Matrix team leaders and members emit Matrix Markdown rules; outside-Matrix teams emit generic Markdown."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    rooms=[],
                    include_default_tools=False,
                ),
            },
            defaults=DefaultsConfig(tools=[], markdown=markdown),
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        ),
        runtime_paths,
    )
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    members = resolve_exact_team_members(
        ["general"],
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=None,
        outside_matrix=outside_matrix,
        refresh_scheduler=None,
        active_model_names={"general": "default"},
    )
    team = build_team_spec(
        members=members,
        mode=TeamMode.COORDINATE,
        config=config,
        runtime_paths=runtime_paths,
        scope_context=unstored_team_scope_context(config, ["general"], team_name=None),
        execution_identity=None,
        model_name="default",
        configured_team_name=None,
        timezone="UTC",
        outside_matrix=outside_matrix,
    )
    member_prompt = system_text(prompt_for_turn(members.build("general", "default")))
    leader_prompt = system_text(team.spec.leader.system_messages)

    for text in (leader_prompt, member_prompt):
        if expected is not None:
            assert expected in text
        if absent is not None:
            assert absent not in text
