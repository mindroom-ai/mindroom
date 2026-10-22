"""Tool-job result payloads in the shape the Agno-based tool executor saved them."""

from __future__ import annotations

from typing import Any

# LEGACY_COMPAT: Tool-job result payloads saved by the Agno-based tool executor.
# Legacy format: the payload's field mapping also holds `elapsed` (the call's duration), `replay` (each drained stream
# item's text length and its Agno run event under a class path such as `agno.run.agent.RunContentEvent`), and
# `control` (the fields of an `AgentRunException` the tool raised, with `Message`-tagged values); its `ToolResult`
# values carry `metadata`, and its media Agno's `media_reference` and, for files, `external` and `citations`.
# Last legacy release: v2026.10.476; replacement: unreleased; the owned executor saves only `value`, `state_delta`,
# and `error`.
# Handling: drop the three fields before decoding, so their tags never decode. The value's text already joins every
# item a stream yielded, which is what the model read, so a call consumed from a legacy payload returns that text; a
# legacy `control` payload kept the exception's text as its `error`. The decoder reads only the fields the owned
# tool value and media model, so the Agno-only keys are ignored.
# Coverage: tests/test_tool_job_results.py::test_a_payload_the_agno_executor_saved_decodes_to_its_text_and_media;
# tests/test_tool_job_results.py::test_a_legacy_control_payload_reads_as_its_error.
_LEGACY_PAYLOAD_FIELDS = frozenset({"elapsed", "replay", "control"})


def drop_legacy_payload_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Return the encoded payload fields without those only the Agno-based executor saved."""
    return {name: value for name, value in fields.items() if name not in _LEGACY_PAYLOAD_FIELDS}
