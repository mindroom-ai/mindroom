"""The owned system-message renderer reproduces the prompt goldens from the inputs MindRoom's builders produce."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.agent_loop.prompt import PromptSpec, render_instructions, render_system_message
from tests.agent_loop.prompt_parity_support import (
    FIXTURE_DIR,
    SCENARIOS,
    load_golden,
    normalize,
    rendered,
    run_scenario,
)

if TYPE_CHECKING:
    from pathlib import Path

# The team leader's prompt belongs to the teams runtime; its scenarios are compared there.
GOLDENS = sorted(path.stem for path in FIXTURE_DIR.glob("*.json") if not path.stem.startswith("team_"))
# Requests whose system message MindRoom writes itself instead of rendering an agent prompt.
MINDROOM_WRITTEN = {("compaction_summary", 3)}


def _golden_system_messages(name: str, golden: dict[str, Any]) -> list[str]:
    messages = [
        request["messages"][0]["content"]
        for index, request in enumerate(golden["requests"])
        if request["messages"][0]["role"] == "system" and (name, index) not in MINDROOM_WRITTEN
    ]
    exported = golden.get("exported") or {}
    # The voice models outside MindRoom's loop receive the agent's chat prompt.
    return messages + [exported[key] for key in ("instructions", "system_prompt") if key in exported]


def test_every_golden_but_the_team_leaders_has_a_scenario() -> None:
    """Each prompt golden this module compares runs through a scenario."""
    assert sorted(SCENARIOS) == GOLDENS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_system_messages_equal_the_goldens(name: str, tmp_path: Path) -> None:
    """Every agent system message renders, from the inputs MindRoom hands the prompt, byte for byte as the golden."""
    golden = load_golden(name)

    run = await run_scenario(name, tmp_path)

    assert SCENARIOS[name].description == golden["description"]
    assert normalize(run.input, tmp_path) == golden["input"]
    assert normalize(run.exported, tmp_path) == golden.get("exported")
    assert run.prompts
    for prompt in run.prompts:
        assert rendered(prompt) == prompt.agno_content
    owned = {normalize(rendered(prompt), tmp_path) for prompt in run.prompts}
    expected = _golden_system_messages(name, golden)
    assert expected
    assert [message for message in expected if message not in owned] == []


def test_instructions_render_as_bullets_unless_one_is_a_block() -> None:
    """One instruction stays verbatim, single-line ones become bullets, and a multi-line one keeps its own layout."""
    assert render_instructions(["Answer briefly.\n"]) == "Answer briefly.\n"
    assert render_instructions(["Answer briefly.", " Cite sources. "]) == "- Answer briefly.\n- Cite sources."
    assert render_instructions(["Answer briefly.", "When unsure:\n- say so\n"]) == (
        "- Answer briefly.\n\nWhen unsure:\n- say so\n"
    )


def test_sections_render_in_agno_order() -> None:
    """Each input lands in its section, in the order and with the separators Agno rendered."""
    spec = PromptSpec(
        description="Persona.",
        role="Role.",
        instructions=("One.", "Two."),
        markdown=True,
        tool_instructions=("Toolkit guidance.", "More guidance."),
        additional_context="<context>",
        skills_block="<skills_system>\n</skills_system>",
        knowledge_block="<knowledge_base>\n</knowledge_base>",
        model_note="Model note.\n\n",
        output_format_prompt="JSON prompt.",
    )

    assert render_system_message(spec).content == (
        "Persona.\n\n<your_role>\nRole.\n</your_role>\n\n- One.\n- Two.\n\n"
        "<additional_information>\n- Use markdown to format your answers.\n</additional_information>\n\n"
        "Toolkit guidance.\nMore guidance.\n<context>\n\n<skills_system>\n</skills_system>\n"
        "<knowledge_base>\n</knowledge_base>\nModel note.\n\nJSON prompt."
    )


def test_an_empty_skills_block_adds_nothing_and_a_system_message_replaces_everything() -> None:
    """Only a non-empty skills block is added, and a set system message is sent verbatim."""
    assert render_system_message(PromptSpec(role="Role.", skills_block="")).content == (
        "<your_role>\nRole.\n</your_role>"
    )
    message = render_system_message(PromptSpec(role="Role.", system_message="  Bootstrap.\n"))
    assert (message.role, message.content) == ("system", "  Bootstrap.\n")
