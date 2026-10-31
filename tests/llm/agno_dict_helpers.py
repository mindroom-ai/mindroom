"""What the owned codecs write for a dict Agno stored: the same dict minus the keys the owned types dropped.

So does every key a message or tool execution holds at its default, which the owned writers leave out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.llm.runs import RunRecord, TeamRunRecord

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.llm.runs import AnyRunRecord

# Keys Agno wrote that the owned ``ToolExecution`` does not model: Agno's user-input HITL variants, the call's
# timing and timestamp, and the silence flag of a call executed outside the run, which nothing read.
DROPPED_TOOL_EXECUTION_KEYS = frozenset(
    {
        "requires_user_input",
        "user_input_schema",
        "user_feedback_schema",
        "answered",
        "approval_id",
        "metrics",
        "created_at",
        "external_execution_silent",
    },
)
# The defaults of the message and tool-execution keys the owned writers store only when they differ, since the reader
# reads an absent key as its default; Agno and every release up to v2026.10.664 stored them all.
MESSAGE_DEFAULTS: dict[str, object] = {"from_history": False, "stop_after_tool_call": False}
TOOL_EXECUTION_DEFAULTS: dict[str, object] = {
    **dict.fromkeys(
        (
            "tool_args",
            "tool_call_error",
            "result",
            "child_run_id",
            "requires_confirmation",
            "confirmed",
            "confirmation_note",
            "external_execution_required",
            "approval_type",
        ),
        None,
    ),
    "stop_after_tool_call": False,
}
# Keys Agno wrote on requirement dicts that the owned ``RunRequirement`` does not model; nothing read its timestamp.
DROPPED_REQUIREMENT_KEYS = frozenset({"created_at"})
# Keys Agno wrote on usage dicts that the owned metrics do not model: ``additional_metrics``, which only tests ever
# filled in, a provider-reported ``cost``, and ``provider_metrics``, of which only a request's ``context_usage`` was
# read (``expected_request_metrics_dict``).
DROPPED_METRICS_KEYS = frozenset({"additional_metrics", "cost", "provider_metrics"})
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


def _without_defaults(data: dict[str, Any], defaults: dict[str, object]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in defaults or value is not defaults[key]}


def expected_message_dict(data: dict[str, Any]) -> dict[str, Any]:
    """A stored message dict minus the flags it holds at their defaults, its metrics as the owned type writes them."""
    result = _without_defaults(data, MESSAGE_DEFAULTS)
    if isinstance(result.get("metrics"), dict):
        result["metrics"] = expected_request_metrics_dict(result["metrics"])
    return result


def expected_metrics_dict(data: dict[str, Any]) -> dict[str, Any]:
    """A stored run or per-model usage dict minus the keys the owned metrics dropped, in each per-model entry too."""
    result = without(data, DROPPED_METRICS_KEYS)
    if isinstance(result.get("details"), dict):
        result["details"] = {
            model_type: [expected_metrics_dict(entry) for entry in entries]
            for model_type, entries in result["details"].items()
        }
    return result


def expected_request_metrics_dict(data: dict[str, Any]) -> dict[str, Any]:
    """A stored request usage dict minus the keys the owned metrics dropped; Claude's ``context_usage`` is its context.

    The context leaves out its zero counters, as every stored usage dict does.
    """
    result = expected_metrics_dict(data)
    provider_metrics = data.get("provider_metrics")
    context = provider_metrics.get("context_usage") if isinstance(provider_metrics, dict) else None
    if isinstance(context, dict):
        result["context"] = {key: value for key, value in context.items() if value != 0}
    return result


def expected_tool_dict(data: dict[str, Any]) -> dict[str, Any]:
    """A stored tool-execution dict minus the keys the owned type dropped and those it holds at their defaults."""
    return _without_defaults(without(data, DROPPED_TOOL_EXECUTION_KEYS), TOOL_EXECUTION_DEFAULTS)


def expected_run_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's run dict minus dropped keys and defaults, recursively over every nested record."""
    result = without(data, DROPPED_RUN_KEYS)
    if isinstance(result.get("metrics"), dict):
        result["metrics"] = expected_metrics_dict(result["metrics"])
    if "messages" in result:
        result["messages"] = [expected_message_dict(message) for message in result["messages"]]
    if "tools" in result:
        result["tools"] = [expected_tool_dict(tool) for tool in result["tools"]]
    if "requirements" in result:
        result["requirements"] = [expected_requirement_dict(item) for item in result["requirements"]]
    if "member_responses" in result:
        result["member_responses"] = [expected_run_dict(member) for member in result["member_responses"]]
    return result


def expected_requirement_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Agno's requirement dict minus the requirement keys the owned type dropped, its tool execution as written."""
    result = without(data, DROPPED_REQUIREMENT_KEYS)
    result["tool_execution"] = expected_tool_dict(result["tool_execution"])
    return result


def stored_run_record(data: Mapping[str, Any]) -> AnyRunRecord:
    """Decode a stored run dict as its row's ``run_type`` does: a ``team_id`` key marks a team run."""
    return TeamRunRecord.from_dict(data) if "team_id" in data else RunRecord.from_dict(data)


def upgraded_run_record(data: Mapping[str, Any]) -> AnyRunRecord:
    """Decode a run dict an earlier release stored as the store reads it once layout version 6 rewrote it.

    A paused notice is no longer its content or its members', and a paused team run's waiting delegation call names
    the paused run of the member it names (see the ``LEGACY_COMPAT`` marker in
    ``mindroom.session_storage.legacy_paused_runs``).
    """
    run = stored_run_record(data)
    members = run.member_responses if isinstance(run, TeamRunRecord) else []
    for record in (run, *members):
        if str(record.content).startswith(PAUSED_NOTICE_OPENINGS):
            record.content = None
    paused_runs = {item.member_agent_id: item.member_run_id for item in run.requirements or ()}
    for tool in run.tools or ():
        if tool.child_run_id is None and str(tool.result).endswith("requires human input before continuing."):
            tool.child_run_id = paused_runs.get((tool.tool_args or {}).get("member_id"))
    return run
