"""Read Dynamic Workflow revisions saved in shapes the current spec no longer accepts."""

from __future__ import annotations

from typing import cast


def upgrade_legacy_spec(spec: dict[str, object]) -> dict[str, object]:
    """Return one freshly read revision with every retired shape read in its current form."""
    return _upgrade_legacy_descriptions(_upgrade_legacy_participants(spec))


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


def _upgrade_legacy_participants(spec: dict[str, object]) -> dict[str, object]:
    """Return the spec with every retired ephemeral participant read as a subagent participant."""
    # LEGACY_COMPAT: Dynamic Workflow ephemeral_agent participants.
    # Legacy format: A saved revision participant with kind ephemeral_agent and optional name, role, instructions, and tools; validation always stored an explicit kind.
    # Last legacy release: v2026.10.230; replacement: the next release saves subagent participants with a system_prompt.
    # Handling: The store renders name, role, and instructions into system_prompt when it reads a revision, keeps id, description, model, and tools, and reads absent tools as no tools, so a revision runs again unless current participant rules refuse it, such as a toolkit that is not pre-approved or one the caller lacks, and the next update writes the current format.
    # Coverage: tests/test_dynamic_workflow_subagents.py::test_legacy_revision_loads_as_subagent, tests/test_dynamic_workflow_subagents.py::test_legacy_revision_runs_as_subagent, tests/test_dynamic_workflow_subagents.py::test_update_of_legacy_revision_writes_current_format.
    participants = spec.get("participants")
    if not isinstance(participants, list):
        return spec
    entries = cast("list[object]", participants)
    if not any(_is_legacy(participant) for participant in entries):
        return spec
    # The store passes a freshly read revision, so its participants can be reused without copying.
    return {
        **spec,
        "participants": [
            _upgrade_participant(cast("dict[str, object]", participant)) if _is_legacy(participant) else participant
            for participant in entries
        ],
    }


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
    upgraded["tools"] = participant.get("tools") or []
    return upgraded


def _upgrade_legacy_descriptions(spec: dict[str, object]) -> dict[str, object]:
    """Return the spec with a saved non-text workflow or input-property description read as its text."""
    # LEGACY_COMPAT: Dynamic Workflow non-string descriptions.
    # Legacy format: A saved revision whose top-level description or inputs.properties.<name>.description is a non-null value other than a string, such as a number, list, or mapping; validation never checked either field, so a revision kept the value the author sent.
    # Last legacy release: v2026.10.696, the newest release when the typed spec landed, because every release since the first Dynamic Workflow release, v2026.6.68, saved both descriptions as sent; replacement: the next release accepts only a string or null there.
    # Handling: The store replaces each such value with str(value), the text a workflow summary already showed, when it reads a revision, keeps null as null, and leaves every other field to the spec rules, so the revision runs again and the next update writes the text.
    # Coverage: tests/test_dynamic_workflows.py::test_revision_with_non_text_descriptions_reads_them_as_text.
    # The store passes a freshly read revision, so its mappings can be changed in place.
    _read_description_as_text(spec)
    inputs = spec.get("inputs")
    properties = cast("dict[str, object]", inputs).get("properties") if isinstance(inputs, dict) else None
    if isinstance(properties, dict):
        for field_schema in cast("dict[str, object]", properties).values():
            if isinstance(field_schema, dict):
                _read_description_as_text(cast("dict[str, object]", field_schema))
    return spec


def _read_description_as_text(data: dict[str, object]) -> None:
    description = data.get("description")
    if description is not None and not isinstance(description, str):
        data["description"] = str(description)
