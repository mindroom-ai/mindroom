"""What the owned codecs write for a dict Agno stored: the same dict minus the keys the owned types dropped.

A run's content that is a paused notice, as Agno and the owned runtime up to v2026.10.651 stored for a run paused
without text, goes too, since the owned reader reads it as no content (``mindroom.llm.legacy_run_content``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord

# Keys Agno wrote that the owned ``ToolExecution`` does not model: Agno's user-input HITL variants, and the call's
# timing and timestamp, which nothing read.
DROPPED_TOOL_EXECUTION_KEYS = frozenset(
    {
        "requires_user_input",
        "user_input_schema",
        "user_feedback_schema",
        "answered",
        "approval_id",
        "metrics",
        "created_at",
    },
)
# Keys Agno wrote on requirement dicts that the owned ``RunRequirement`` does not model; nothing read its timestamp.
DROPPED_REQUIREMENT_KEYS = frozenset({"created_at"})
# Keys Agno wrote on run and session metrics that the owned metrics do not model; only tests ever filled them in.
DROPPED_METRICS_KEYS = frozenset({"additional_metrics"})
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
        "team_name",
    },
)


# How every paused notice a stored run's content held begins: an agent run's, then a team run's.
PAUSED_NOTICE_OPENINGS = ("I have tools to execute, but ", "Team run paused.")


def without(data: dict[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    """Return ``data`` without ``keys``."""
    return {key: value for key, value in data.items() if key not in keys}


def expected_run_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's run dict minus dropped keys and paused notices, recursively over metrics, tools, requirements, and members."""
    result = without(data, DROPPED_RUN_KEYS)
    if isinstance(result.get("content"), str) and result["content"].startswith(PAUSED_NOTICE_OPENINGS):
        del result["content"]
    if isinstance(result.get("metrics"), dict):
        result["metrics"] = without(result["metrics"], DROPPED_METRICS_KEYS)
    if "tools" in result:
        result["tools"] = [without(tool, DROPPED_TOOL_EXECUTION_KEYS) for tool in result["tools"]]
    if "requirements" in result:
        result["requirements"] = [expected_requirement_dict(item) for item in result["requirements"]]
    if "member_responses" in result:
        result["member_responses"] = [expected_run_dict(member) for member in result["member_responses"]]
    return result


def expected_requirement_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's requirement dict minus the requirement and tool-execution keys the owned types dropped."""
    result = without(data, DROPPED_REQUIREMENT_KEYS)
    result["tool_execution"] = without(result["tool_execution"], DROPPED_TOOL_EXECUTION_KEYS)
    return result


def stored_run_record(data: Mapping[str, Any]) -> AnyRunRecord:
    """Decode a stored run dict as its row's ``run_type`` does: a ``team_id`` key marks a team run."""
    return TeamRunRecord.from_dict(data) if "team_id" in data else RunRecord.from_dict(data)
