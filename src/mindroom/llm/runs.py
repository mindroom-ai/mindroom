"""Run records, tool executions, and pause requirements, with the dict shapes stored in run rows.

Keys and ``RunStatus`` strings are Agno 3's, so existing run rows, summary-row segments, and
delegation state decode without migration; keys the records do not model are ignored on read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.llm.messages import Message
from mindroom.llm.usage import RunMetrics, ToolCallMetrics

if TYPE_CHECKING:
    from collections.abc import Mapping


def _new_id() -> str:
    return str(uuid4())


def _now() -> int:
    return int(time())


class RunStatus(StrEnum):
    """Lifecycle state of a run; the values are the stored ``status`` strings."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass(kw_only=True)
class ToolExecution:
    """One tool call the model requested, with its outcome or pause state."""

    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_call_error: bool | None = None
    result: str | None = None
    metrics: ToolCallMetrics | None = None
    child_run_id: str | None = None
    stop_after_tool_call: bool = False
    created_at: int = field(default_factory=_now)
    requires_confirmation: bool | None = None
    confirmed: bool | None = None
    confirmation_note: str | None = None
    external_execution_required: bool | None = None
    external_execution_silent: bool | None = None
    approval_type: str | None = None

    @property
    def is_paused(self) -> bool:
        """Whether the run must wait before this call runs or resumes."""
        return bool(self.requires_confirmation or self.external_execution_required)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict: every field, None included, as Agno wrote it."""
        values = asdict(self)
        values["metrics"] = self.metrics.to_dict() if self.metrics is not None else None
        return values

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolExecution:
        """Rebuild from a stored dict, ignoring keys this type does not model."""
        metrics = data.get("metrics")
        created_at = {"created_at": data["created_at"]} if "created_at" in data else {}
        return cls(
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
            tool_args=data.get("tool_args"),
            tool_call_error=data.get("tool_call_error"),
            result=data.get("result"),
            metrics=ToolCallMetrics.from_dict(metrics) if metrics else None,
            child_run_id=data.get("child_run_id"),
            stop_after_tool_call=data.get("stop_after_tool_call", False),
            requires_confirmation=data.get("requires_confirmation"),
            confirmed=data.get("confirmed"),
            confirmation_note=data.get("confirmation_note"),
            external_execution_required=data.get("external_execution_required"),
            external_execution_silent=data.get("external_execution_silent"),
            approval_type=data.get("approval_type"),
            **created_at,
        )


@dataclass
class RunRequirement:
    """What a paused run waits for: a confirmation or an external execution result for one tool call."""

    tool_execution: ToolExecution
    id: str = field(default_factory=_new_id, kw_only=True)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC), kw_only=True)
    confirmation: bool | None = field(default=None, kw_only=True)
    confirmation_note: str | None = field(default=None, kw_only=True)
    external_execution_result: str | None = field(default=None, kw_only=True)
    member_agent_id: str | None = field(default=None, kw_only=True)
    member_agent_name: str | None = field(default=None, kw_only=True)
    member_run_id: str | None = field(default=None, kw_only=True)

    @property
    def needs_confirmation(self) -> bool:
        """Whether a confirmation decision is still outstanding."""
        if self.confirmation is not None or self.tool_execution.confirmed is not None:
            return False
        return bool(self.tool_execution.requires_confirmation)

    @property
    def needs_external_execution(self) -> bool:
        """Whether an external execution result is still outstanding."""
        if self.external_execution_result is not None:
            return False
        return bool(self.tool_execution.external_execution_required)

    def is_resolved(self) -> bool:
        """Whether nothing is outstanding."""
        return not self.needs_confirmation and not self.needs_external_execution

    def confirm(self) -> None:
        """Approve the call; both this requirement and its tool execution record the decision."""
        if not self.needs_confirmation:
            msg = "This requirement does not require confirmation"
            raise ValueError(msg)
        self.confirmation = True
        self.tool_execution.confirmed = True

    def reject(self, note: str | None = None) -> None:
        """Deny the call with an optional note shown to the model."""
        if not self.needs_confirmation:
            msg = "This requirement does not require confirmation"
            raise ValueError(msg)
        self.confirmation = False
        self.confirmation_note = note
        self.tool_execution.confirmed = False
        self.tool_execution.confirmation_note = note

    def set_external_execution_result(self, result: str) -> None:
        """Record the result an external executor produced for the call."""
        if not self.needs_external_execution:
            msg = "This requirement does not require external execution"
            raise ValueError(msg)
        self.external_execution_result = result
        self.tool_execution.result = result

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict; ``created_at`` is ISO 8601 and None values are dropped."""
        values = {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "confirmation": self.confirmation,
            "confirmation_note": self.confirmation_note,
            "external_execution_result": self.external_execution_result,
            "member_agent_id": self.member_agent_id,
            "member_agent_name": self.member_agent_name,
            "member_run_id": self.member_run_id,
            "tool_execution": self.tool_execution.to_dict(),
        }
        return {key: value for key, value in values.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRequirement:
        """Rebuild from a stored dict, ignoring the user-input keys Agno's HITL variants carried."""
        identifier = data.get("id")
        created_at = data.get("created_at")
        return cls(
            ToolExecution.from_dict(data["tool_execution"]),
            id=identifier if isinstance(identifier, str) else _new_id(),
            created_at=datetime.fromisoformat(created_at) if isinstance(created_at, str) else datetime.now(UTC),
            confirmation=data.get("confirmation"),
            confirmation_note=data.get("confirmation_note"),
            external_execution_result=data.get("external_execution_result"),
            member_agent_id=data.get("member_agent_id"),
            member_agent_name=data.get("member_agent_name"),
            member_run_id=data.get("member_run_id"),
        )


@dataclass(kw_only=True)
class _RunRecordCommon:
    """Fields shared by agent and team run records."""

    run_id: str
    session_id: str | None = None
    parent_run_id: str | None = None
    user_id: str | None = None
    status: RunStatus = RunStatus.RUNNING
    content: str | dict[str, Any] | None = None
    model: str | None = None
    model_provider: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolExecution] = field(default_factory=list)
    requirements: list[RunRequirement] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)
    metadata: dict[str, Any] | None = None
    created_at: int = field(default_factory=_now)

    def _common_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "user_id": self.user_id,
            "content": self.content,
            "model": self.model,
            "model_provider": self.model_provider,
            "metadata": self.metadata,
        }
        result = {key: value for key, value in values.items() if value is not None}
        result["created_at"] = self.created_at
        result["status"] = self.status.value
        result["metrics"] = self.metrics.to_dict()
        result["messages"] = [message.to_dict() for message in self.messages]
        if self.tools:
            result["tools"] = [tool.to_dict() for tool in self.tools]
        if self.requirements:
            result["requirements"] = [requirement.to_dict() for requirement in self.requirements]
        return result


def _common_from_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    # LEGACY_COMPAT: Agno 3 run dicts carry keys the owned run records dropped.
    # Legacy format: ``run_data`` rows and summary-row ``member_responses`` written by the Agno-backed runtime
    # may hold ``input``, ``content_type``, ``session_state``, ``events``, ``images``/``videos``/``audio``/
    # ``files``, ``response_audio``, ``citations``, ``references``, ``reasoning_*``, ``followups``,
    # ``model_provider_data``, fork and queue lineage fields, and per-tool ``requires_user_input``,
    # ``user_input_schema``, ``user_feedback_schema``, ``answered``, ``approval_id``.
    # Last legacy release: every release pinned to agno 3.0.9, up to the last one before the release that
    # removes Agno (not yet tagged when this reader was written); replaced by these codecs in that release.
    # Handling: the keys are ignored on read and never written again; every key MindRoom reads survives.
    # Coverage: tests/llm/test_codecs.py::test_run_record_round_trips_agno_dict_minus_dropped_keys
    values: dict[str, Any] = {
        "run_id": data["run_id"],
        "session_id": data.get("session_id"),
        "parent_run_id": data.get("parent_run_id"),
        "user_id": data.get("user_id"),
        "status": RunStatus(data.get("status", RunStatus.RUNNING.value)),
        "content": data.get("content"),
        "model": data.get("model"),
        "model_provider": data.get("model_provider"),
        "messages": [Message.from_dict(message) for message in data.get("messages") or []],
        "tools": [ToolExecution.from_dict(tool) for tool in data.get("tools") or []],
        "requirements": [RunRequirement.from_dict(item) for item in data.get("requirements") or []],
        "metrics": RunMetrics.from_dict(data.get("metrics") or {}),
        "metadata": data.get("metadata"),
    }
    if "created_at" in data:
        values["created_at"] = data["created_at"]
    return values


@dataclass(kw_only=True)
class RunRecord(_RunRecordCommon):
    """A persisted agent run; a team member run carries the team run as ``parent_run_id``."""

    agent_id: str | None = None
    agent_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stored ``run_data`` dict."""
        result = self._common_dict()
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.agent_name is not None:
            result["agent_name"] = self.agent_name
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRecord:
        """Rebuild from a stored ``run_data`` dict, ignoring keys this record does not model."""
        return cls(agent_id=data.get("agent_id"), agent_name=data.get("agent_name"), **_common_from_dict(data))


@dataclass(kw_only=True)
class TeamRunRecord(_RunRecordCommon):
    """A persisted team run; paused member runs ride along in ``member_responses``."""

    team_id: str | None = None
    team_name: str | None = None
    member_responses: list[RunRecord | TeamRunRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored ``run_data`` dict; ``member_responses`` is always present."""
        result = self._common_dict()
        if self.team_id is not None:
            result["team_id"] = self.team_id
        if self.team_name is not None:
            result["team_name"] = self.team_name
        result["member_responses"] = [member.to_dict() for member in self.member_responses]
        return result

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TeamRunRecord:
        """Rebuild from a stored ``run_data`` dict, ignoring keys this record does not model."""
        return cls(
            team_id=data.get("team_id"),
            team_name=data.get("team_name"),
            member_responses=[run_record_from_dict(member) for member in data.get("member_responses") or []],
            **_common_from_dict(data),
        )


type AnyRunRecord = RunRecord | TeamRunRecord


def run_record_from_dict(data: Mapping[str, Any]) -> AnyRunRecord:
    """Rebuild whichever record a stored dict holds.

    A ``team_id`` key marks a team run; everything else is an agent run, including the usage-only records
    MindRoom writes without an ``agent_id`` (Agno keyed on ``agent_id`` and would have read those as team runs).
    """
    if "team_id" in data:
        return TeamRunRecord.from_dict(data)
    return RunRecord.from_dict(data)


__all__ = [
    "AnyRunRecord",
    "RunRecord",
    "RunRequirement",
    "RunStatus",
    "TeamRunRecord",
    "ToolExecution",
    "run_record_from_dict",
]
