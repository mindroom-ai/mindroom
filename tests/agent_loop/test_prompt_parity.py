"""The owned path reproduces the prompt goldens: every request of each helper call, Matrix reply, and voice call.

Helper call sites, Matrix replies, and voice calls send their requests through the owned provider layer, so one
recorder compares every request they send, byte for byte, with the golden, beside whatever a voice call exports to
the realtime model. The team goldens are compared in ``tests/team_runtime/test_prompt_parity.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.prompt import PromptSpec, render_instructions, render_system_message
from mindroom.llm.messages import Message
from tests.agno_parity.prompt_scenarios import FIXTURE_DIR, SCENARIOS, Recorder, call_dict, load_golden, normalize
from tests.providers.golden_support import ordered_json

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))
TEAM = [name for name in GOLDENS if name.startswith("team_")]
RECORDED = [name for name in GOLDENS if name not in TEAM]


def test_every_golden_is_compared_here_or_by_the_team_runtime() -> None:
    """Each prompt golden is compared by this module or by the team runtime."""
    assert set(GOLDENS) == set(SCENARIOS)
    assert sorted({*RECORDED, *TEAM}) == GOLDENS


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
    exported = normalize(output.exported, tmp_path)
    assert exported == golden.get("exported")
    assert ordered_json(exported) == ordered_json(golden.get("exported"))
    # The realtime call's model is LiveKit's, so its golden pins only what the call exports to it.
    assert recorder.calls or output.exported is not None
    ordered = recorder.ordered_calls()
    # Every request, the compaction summary included, went through the provider registry rather than an Agno model.
    assert all(isinstance(message, Message) for call in ordered for message in call.messages)
    requests = normalize([call_dict(call) for call in ordered], tmp_path)
    assert requests == golden["requests"]
    assert ordered_json(requests) == ordered_json(golden["requests"])


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


def test_an_empty_skills_block_adds_nothing() -> None:
    """Only a non-empty skills block is added."""
    assert render_system_message(PromptSpec(role="Role.", skills_block="")).content == (
        "<your_role>\nRole.\n</your_role>"
    )
