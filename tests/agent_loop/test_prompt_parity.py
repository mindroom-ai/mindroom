"""The owned path reproduces the prompt goldens: every request of each helper call, Matrix reply, and voice call.

Helper call sites, Matrix replies, and voice calls send their requests through the owned provider layer, so one
recorder compares every request they send, byte for byte, with the golden, beside whatever a voice call exports to
the realtime model. The team goldens are compared in ``tests/teams/test_prompt_parity.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.prompt import (
    MARKDOWN_OUTPUT_FORMAT,
    MATRIX_MARKDOWN_OUTPUT_FORMAT,
    MATRIX_TOOL_FOLDING_FORMAT,
    PromptSpec,
    render_instructions,
    render_system_messages,
)
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.llm.messages import Message, system_text
from mindroom.tool_system.plugins import config_plugins
from tests.agno_parity.golden_files import render
from tests.agno_parity.prompt_scenarios import (
    FIXTURE_DIR,
    SCENARIOS,
    golden_path,
    load_golden,
    record_scenario,
    regenerated_golden,
)
from tests.conftest import bind_runtime_paths, install_model_providers, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))
TEAM = [name for name in GOLDENS if name.startswith("team_")]
RECORDED = [name for name in GOLDENS if name not in TEAM]


def test_every_golden_is_compared_here_or_by_the_team_prompt_tests() -> None:
    """Each prompt golden is compared by this module or by ``tests/teams/test_prompt_parity.py``."""
    assert set(GOLDENS) == set(SCENARIOS)
    assert sorted({*RECORDED, *TEAM}) == GOLDENS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", RECORDED)
async def test_recorded_requests_equal_the_goldens(name: str, tmp_path: Path) -> None:
    """Each entry point sends its golden requests byte for byte, through the owned provider layer.

    The golden also holds what a scenario hands a model outside MindRoom's loop, such as a realtime call's
    instructions and tools; the realtime call's model is LiveKit's, so its golden pins only what the call exports.
    """
    recorded = await record_scenario(name, tmp_path)

    regenerated = regenerated_golden(name, recorded, tmp_path)
    assert regenerated == load_golden(name)
    assert render(regenerated) == golden_path(name).read_text(encoding="utf-8")
    assert recorded.calls or recorded.output.exported is not None
    # Every request, the compaction summary included, went through the provider registry rather than an Agno model.
    assert all(isinstance(message, Message) for call in recorded.calls for message in call.messages)


def test_instructions_render_as_bullets_unless_one_is_a_block() -> None:
    """One instruction stays verbatim, single-line ones become bullets, and a multi-line one keeps its own layout.

    Each is stripped, so a block that ends with a newline is still one blank line from the next.
    """
    assert render_instructions(["Answer briefly.\n"]) == "Answer briefly.\n"
    assert render_instructions(["Answer briefly.", " Cite sources. "]) == "- Answer briefly.\n- Cite sources."
    assert render_instructions(["Answer briefly.", "When unsure:\n- say so\n"]) == (
        "- Answer briefly.\n\nWhen unsure:\n- say so"
    )
    assert render_instructions(["When unsure:\n- say so\n", "Then:\n- stop\n"]) == (
        "When unsure:\n- say so\n\nThen:\n- stop"
    )


def test_sections_render_as_a_stable_part_and_a_turn_part() -> None:
    """Each input lands in its section, one blank line apart; the turn context is its own system message, last."""
    spec = PromptSpec(
        persona="Persona.\n",
        role="Role.",
        instructions=("One.", "Two."),
        markdown=True,
        tool_instructions=("Toolkit guidance.\n", "More guidance."),
        turn_context="<context>\n",
        skills_block="<skills_system>\n</skills_system>",
        knowledge_block="<knowledge_base>\n</knowledge_base>",
    )

    stable, turn = render_system_messages(spec)
    assert stable.content == (
        f"Persona.\n\n<your_role>\nRole.\n</your_role>\n\n{MARKDOWN_OUTPUT_FORMAT}\n\n- One.\n- Two.\n\n"
        "Toolkit guidance.\n\nMore guidance.\n\n<skills_system>\n</skills_system>\n\n"
        "<knowledge_base>\n</knowledge_base>"
    )
    assert (stable.role, turn.role, turn.content) == ("system", "system", "<context>")


def test_a_changed_turn_context_leaves_the_stable_part_byte_identical() -> None:
    """Only the turn part carries the turn's context, so the cacheable stable part is the same every turn."""
    spec = PromptSpec(role="Role.", instructions=("One.",), turn_context="Monday.")

    monday = render_system_messages(spec)
    tuesday = render_system_messages(PromptSpec(role="Role.", instructions=("One.",), turn_context="Tuesday."))

    assert monday[0].content == tuesday[0].content == "<your_role>\nRole.\n</your_role>\n\nOne."
    assert (monday[1].content, tuesday[1].content) == ("Monday.", "Tuesday.")


def test_empty_sections_and_parts_add_nothing() -> None:
    """An empty skills block or a blank role adds no section, and a prompt without turn context sends one message."""
    [stable] = render_system_messages(PromptSpec(role="Role.", skills_block="", instructions=()))
    assert stable.content == "<your_role>\nRole.\n</your_role>"
    [turn] = render_system_messages(PromptSpec(role=" ", instructions=(), turn_context="Today."))
    assert turn.content == "Today."


def test_agent_markdown_format_uses_matrix_rules_only_inside_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix agents receive Matrix Markdown rules; outside-Matrix agents receive generic Markdown guidance."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General assistant",
                    rooms=[],
                    include_default_tools=False,
                    markdown=True,
                ),
                "plain": AgentConfig(
                    display_name="PlainAgent",
                    role="Plain assistant",
                    rooms=[],
                    include_default_tools=False,
                    markdown=False,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        ),
        runtime_paths,
    )
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    plugins = config_plugins(config, runtime_paths)

    matrix_build = create_agent(
        "general",
        config,
        runtime_paths,
        execution_identity=None,
        model=turn_model(config, runtime_paths, "general"),
        knowledge=None,
        plugins=plugins,
        outside_matrix=False,
    )
    outside_build = create_agent(
        "general",
        config,
        runtime_paths,
        execution_identity=None,
        model=turn_model(config, runtime_paths, "general"),
        knowledge=None,
        plugins=plugins,
        outside_matrix=True,
    )
    plain_build = create_agent(
        "plain",
        config,
        runtime_paths,
        execution_identity=None,
        model=turn_model(config, runtime_paths, "plain"),
        knowledge=None,
        plugins=plugins,
        outside_matrix=False,
    )

    matrix_text = system_text(prompt_for_turn(matrix_build))
    outside_text = system_text(prompt_for_turn(outside_build))
    plain_text = system_text(prompt_for_turn(plain_build))

    assert MATRIX_MARKDOWN_OUTPUT_FORMAT in matrix_text
    assert "add a language tag to fenced code blocks" in matrix_text
    assert "GFM tables" in matrix_text
    assert "Mermaid fences (```mermaid)" in matrix_text
    assert "GitHub callouts (`> [!NOTE]`, `TIP`, `IMPORTANT`, `WARNING`, `CAUTION`)" in matrix_text
    assert "footnotes (`[^1]` / `[^1]: ...`)" in matrix_text
    assert "External image URLs (`![](https://...)`) are not rendered; link to external URLs instead." in matrix_text
    assert "blank lines around inner Markdown" in matrix_text
    assert "<span data-mx-spoiler>...</span>" in matrix_text
    assert "<https://matrix.to/#/!room_id>" in matrix_text
    assert "<https://matrix.to/#/!room_id/$event_id>" in matrix_text
    assert "unbackticked" in matrix_text
    # Tool-less Matrix agents omit the tool-folding rule.
    assert MATRIX_TOOL_FOLDING_FORMAT not in matrix_text
    assert MARKDOWN_OUTPUT_FORMAT in outside_text
    assert MATRIX_MARKDOWN_OUTPUT_FORMAT not in outside_text
    assert "Mermaid fences" not in outside_text
    assert "<span data-mx-spoiler>" not in outside_text
    assert "<https://matrix.to/#/!room_id>" not in outside_text
    assert MATRIX_TOOL_FOLDING_FORMAT not in outside_text
    assert MARKDOWN_OUTPUT_FORMAT not in plain_text
    assert MATRIX_TOOL_FOLDING_FORMAT not in plain_text

    config.agents["general"].tools = ["calculator"]
    tool_build = create_agent(
        "general",
        config,
        runtime_paths,
        execution_identity=None,
        model=turn_model(config, runtime_paths, "general"),
        knowledge=None,
        plugins=plugins,
        outside_matrix=False,
    )
    tool_text = system_text(prompt_for_turn(tool_build))
    assert MATRIX_MARKDOWN_OUTPUT_FORMAT in tool_text
    assert MATRIX_TOOL_FOLDING_FORMAT in tool_text
    assert "self-contained final answer after your last tool call" not in plain_text
