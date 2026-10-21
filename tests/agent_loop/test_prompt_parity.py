"""The owned path reproduces the prompt goldens: every request of each helper call, Matrix reply, and voice call.

Helper call sites, Matrix replies, and voice calls send their requests through the owned provider layer, so one
recorder compares every request they send, byte for byte, with the golden, beside whatever a voice call exports to
the realtime model. The goldens of the entry points that do not run on the owned path yet are listed with the step
that will compare them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.prompt import PromptSpec, render_instructions, render_system_message
from mindroom.llm.messages import Message
from tests.agno_parity.prompt_scenarios import FIXTURE_DIR, SCENARIOS, Recorder, call_dict, load_golden, normalize

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))
# The owner of each golden whose entry point does not run on the owned path yet.
PENDING = {
    "agent_minimal_mode": "F3 renders minimal mode's bootstrap prompt and Bash facade from the build's surface",
    "delegated_child_run": "S3 drives the delegate tool's externally executed children through the owned loop",
    "dynamic_workflow_room_agent": "F6 runs workflow participants on the owned loop",
    "openai_compat_api": "F5 serves the OpenAI-compatible API from the owned loop",
    "skill_review_fork": "F6 runs the skill review fork on the owned loop from the captured final request",
}
TEAM = [name for name in GOLDENS if name.startswith("team_")]
RECORDED = [name for name in GOLDENS if name not in PENDING and name not in TEAM]
# Requests an Agno model still sends, by position in the golden: F7 moves the compaction summarizer.
AGNO_REQUESTS = {"compaction_summary": {3}}


def test_every_golden_is_compared_here_or_assigned_an_owner() -> None:
    """Each prompt golden is compared by this module, by the team runtime, or named with the step that will."""
    assert set(GOLDENS) == set(SCENARIOS)
    assert sorted({*RECORDED, *PENDING, *TEAM}) == GOLDENS


@pytest.mark.asyncio
@pytest.mark.parametrize("name", RECORDED)
async def test_recorded_requests_equal_the_goldens(name: str, tmp_path: Path) -> None:
    """Each entry point sends its golden requests byte for byte, through the owned provider layer."""
    golden = load_golden(name)
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)

    with recorder.installed():
        output = await scenario.run(tmp_path)

    assert scenario.description == golden["description"]
    assert normalize(output.input, tmp_path) == golden["input"]
    # What a scenario hands a model outside MindRoom's loop, such as a realtime call's instructions and tools.
    assert normalize(output.exported, tmp_path) == golden.get("exported")
    # The realtime call's model is LiveKit's, so its golden pins only what the call exports to it.
    assert recorder.calls or output.exported is not None
    ordered = recorder.ordered_calls()
    # Every other request went through the provider registry rather than an Agno model.
    assert {index for index, call in enumerate(ordered) if not call.owned} == AGNO_REQUESTS.get(name, set())
    assert all(isinstance(message, Message) for call in ordered if call.owned for message in call.messages)
    assert normalize([call_dict(call) for call in ordered], tmp_path) == golden["requests"]


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
