"""Read Dynamic Workflow revisions saved before participants became authored subagents."""

from __future__ import annotations

import copy
from typing import cast


def _render_legacy_participant_prompt(name: object, role: object, instructions: object) -> str:
    """Render one retired participant's name, role, and instructions as a system prompt."""
    if isinstance(instructions, str):
        lines = [instructions]
    elif isinstance(instructions, list):
        lines = [str(line) for line in cast("list[object]", instructions)]
    else:
        lines = []
    parts = [
        f"You are {name}." if isinstance(name, str) and name else "",
        role if isinstance(role, str) else "",
        "\n".join(f"- {line}" for line in lines if line),
    ]
    return "\n\n".join(part for part in parts if part) or "You are a Dynamic Workflow participant."


def upgrade_legacy_participants(spec: dict[str, object]) -> dict[str, object]:
    """Return the spec with every retired ephemeral participant read as a subagent participant."""
    # LEGACY_COMPAT: Dynamic Workflow ephemeral_agent participants.
    # Legacy format: A saved revision participant with kind ephemeral_agent and optional name, role, instructions, and tools; validation always stored an explicit kind.
    # Last legacy release: v2026.10.222; replacement: the next release saves subagent participants with a system_prompt.
    # Handling: The store renders name, role, and instructions into system_prompt when it reads a revision, keeps id, description, model, and tools, and reads absent tools as no tools, so a revision runs again unless current participant rules refuse it, such as a toolkit that is not pre-approved or one the caller lacks, and the next update writes the current format.
    # Coverage: tests/test_dynamic_workflow_subagents.py::test_legacy_revision_loads_as_subagent, tests/test_dynamic_workflow_subagents.py::test_update_of_legacy_revision_writes_current_format.
    participants = spec.get("participants")
    if not isinstance(participants, list):
        return spec
    entries = cast("list[object]", participants)
    if not any(_is_legacy(participant) for participant in entries):
        return spec
    upgraded = copy.deepcopy(spec)
    upgraded["participants"] = [
        _upgrade_participant(cast("dict[str, object]", participant)) if _is_legacy(participant) else participant
        for participant in copy.deepcopy(entries)
    ]
    return upgraded


def _is_legacy(participant: object) -> bool:
    return isinstance(participant, dict) and cast("dict[str, object]", participant).get("kind") == "ephemeral_agent"


def _upgrade_participant(participant: dict[str, object]) -> dict[str, object]:
    upgraded: dict[str, object] = {
        "id": participant.get("id"),
        "kind": "subagent",
        "system_prompt": _render_legacy_participant_prompt(
            participant.get("name"),
            participant.get("role"),
            participant.get("instructions"),
        ),
    }
    for key in ("description", "model"):
        if participant.get(key) is not None:
            upgraded[key] = participant[key]
    upgraded["tools"] = copy.deepcopy(participant.get("tools") or [])
    return upgraded
