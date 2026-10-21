"""What the owned codecs write for a dict Agno stored: the same dict minus the keys the owned types dropped."""

from __future__ import annotations

from typing import Any

# Keys Agno wrote that the owned ``ToolExecution`` does not model (Agno's user-input HITL variants).
DROPPED_TOOL_EXECUTION_KEYS = frozenset(
    {"requires_user_input", "user_input_schema", "user_feedback_schema", "answered", "approval_id"},
)
# Keys Agno wrote on run dicts that the owned records do not model; nothing in MindRoom reads them.
DROPPED_RUN_KEYS = frozenset(
    {
        "content_type",
        "input",
        "session_state",
        "model_provider_data",
        "reasoning_content",
        "images",
        "videos",
        "audio",
        "files",
        "response_audio",
        "references",
        "citations",
        "followups",
        "events",
    },
)


def without(data: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    """Return ``data`` without ``keys``."""
    return {key: value for key, value in data.items() if key not in keys}


def expected_run_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's run dict minus dropped keys, recursively over tools, requirements, and member runs."""
    result = without(data, DROPPED_RUN_KEYS)
    if "tools" in result:
        result["tools"] = [without(tool, DROPPED_TOOL_EXECUTION_KEYS) for tool in result["tools"]]
    if "requirements" in result:
        result["requirements"] = [expected_requirement_dict(item) for item in result["requirements"]]
    if "member_responses" in result:
        result["member_responses"] = [expected_run_dict(member) for member in result["member_responses"]]
    return result


def expected_requirement_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's requirement dict minus the tool-execution keys the owned type dropped."""
    result = dict(data)
    result["tool_execution"] = without(result["tool_execution"], DROPPED_TOOL_EXECUTION_KEYS)
    return result
