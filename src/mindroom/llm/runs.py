# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Run records, tool executions, and pause requirements, with the dict shapes stored in run rows.

Keys and ``RunStatus`` strings are those existing run rows, summary-row segments, and delegation state hold, so they
decode without migration; keys the records do not model are ignored on read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from time import time
from typing import Any
from uuid import uuid4

from mindroom.llm.messages import Message
from mindroom.llm.usage import RunMetrics, ToolCallMetrics, known_fields
from mindroom.redaction import redact_sensitive_text


def _new_id() -> str:
    return str(uuid4())


def _now() -> int:
    return int(time())


class RunStatus(StrEnum):
    """Lifecycle state of a run; the values are the stored ``status`` strings.

    The members have lower-case names because ``.name`` is persisted too (``mindroom_original_status`` in run metadata
    holds ``"cancelled"``, not ``"CANCELLED"``). MindRoom never writes ``PENDING`` or ``REGENERATED``; they are members
    so a stored row written with them still reads.
    """

    pending = "PENDING"
    running = "RUNNING"
    completed = "COMPLETED"
    paused = "PAUSED"
    cancelled = "CANCELLED"
    error = "ERROR"
    regenerated = "REGENERATED"


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
        """Return the stored dict: every field, None included, kept as main writes it."""
        values = asdict(self)
        values["metrics"] = self.metrics.to_dict() if self.metrics is not None else None
        return values

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolExecution:
        """Rebuild from a stored dict, ignoring keys this type does not model; an absent key keeps its default."""
        values = known_fields(cls, data)
        metrics = values.pop("metrics", None)
        return cls(**values, metrics=ToolCallMetrics.from_dict(metrics) if metrics else None)


_DENIED_WITHOUT_REASON = "Not approved by requester"


def denial_note_for(reason: str | None) -> str:
    """Return the note the model is told for a denied call: the requester's reason, or that it was not approved."""
    return reason or _DENIED_WITHOUT_REASON


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

    @property
    def denial_note(self) -> str:
        """The note a denied call answers the model with, which ``reject`` always records."""
        if self.confirmation is not False or self.confirmation_note is None:
            msg = "This requirement is not a recorded denial"
            raise ValueError(msg)
        return self.confirmation_note

    def confirm(self) -> None:
        """Approve the call; both this requirement and its tool execution record the decision."""
        if not self.needs_confirmation:
            msg = "This requirement does not require confirmation"
            raise ValueError(msg)
        self.confirmation = True
        self.tool_execution.confirmed = True

    def reject(self, note: str | None) -> None:
        """Deny the call; the model is told ``note``, or that the requester did not approve when it gave none."""
        if not self.needs_confirmation:
            msg = "This requirement does not require confirmation"
            raise ValueError(msg)
        note = denial_note_for(note)
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
        """Rebuild from a stored dict, ignoring keys this type does not model, such as older rows' user-input keys.

        A missing tool execution becomes an ``unknown`` placeholder, an unreadable ``created_at`` becomes now, and a
        decision recorded only on the requirement reaches its tool execution, which is what a resume runs from.
        """
        # LEGACY_COMPAT: Run requirements stored without a tool execution or with an unreadable timestamp.
        # Legacy format: ``requirements`` entries lacking ``tool_execution``, or whose ``created_at`` is not
        # ISO 8601, which Agno's reader accepted as legacy data.
        # Last legacy release: unversioned external input; no MindRoom release is known to write them, and
        # agno 3.0.9 always wrote both.
        # Handling: the entry reads with an ``unknown`` placeholder tool execution and the current time, as
        # Agno's reader did, so one such entry never fails the load of its session.
        # Coverage: tests/llm/test_codecs.py::test_run_requirement_reads_rows_agno_read_leniently
        tool_data = data.get("tool_execution")
        requirement = cls(
            ToolExecution.from_dict(tool_data)
            if isinstance(tool_data, Mapping)
            else ToolExecution(tool_name="unknown", tool_args={}),
            id=identifier if isinstance(identifier := data.get("id"), str) else _new_id(),
            created_at=_datetime_or_now(data.get("created_at")),
            confirmation=data.get("confirmation"),
            confirmation_note=data.get("confirmation_note"),
            external_execution_result=data.get("external_execution_result"),
            member_agent_id=data.get("member_agent_id"),
            member_agent_name=data.get("member_agent_name"),
            member_run_id=data.get("member_run_id"),
        )
        execution = requirement.tool_execution
        if requirement.confirmation is not None and execution.confirmed is None:
            execution.confirmed = requirement.confirmation
            if requirement.confirmation is False and requirement.confirmation_note is not None:
                execution.confirmation_note = requirement.confirmation_note
        if requirement.external_execution_result is not None and execution.result is None:
            execution.result = requirement.external_execution_result
        return requirement


def _datetime_or_now(value: object) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


@dataclass(kw_only=True)
class _RunRecordCommon:
    """Fields shared by agent and team run records."""

    run_id: str
    session_id: str | None = None
    parent_run_id: str | None = None
    user_id: str | None = None
    status: RunStatus = RunStatus.running
    content: str | None = None
    model: str | None = None
    model_provider: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolExecution] | None = None
    requirements: list[RunRequirement] | None = None
    metrics: RunMetrics | None = None
    metadata: dict[str, Any] | None = None
    created_at: int = field(default_factory=_now)
    # Why an ``ERROR`` run failed, credentials redacted, set by the loop that ended it. It is never stored, so a
    # loaded record has none; the stored ``content`` holds the error's text only when the model said nothing.
    error: str | None = field(default=None, compare=False, repr=False)
    # The content a resumed run carried over from its pause, set by the resume (``resumed_record``): a resumed agent
    # or team member keeps its paused content until it writes text of its own, while a resumed team leader starts its
    # text afresh (``continue_team``) and this records what it had at the pause. It is never stored, so a loaded
    # record has none.
    carried_content: str | None = field(default=None, compare=False, repr=False)

    @property
    def handover_content(self) -> str | None:
        """Return what the run shows whoever reads it after it ends: why it failed, else its content.

        The run that started it, a team's document of its members, and a delegation's settlement all read this. A
        failed run that ended in this process shows its ``error``; a failed record loaded from storage has none, so
        its stored content, the unredacted error when its model said nothing, is shown with credentials redacted.
        """
        if self.status is not RunStatus.error:
            return self.content
        if self.error is not None:
            return self.error
        return None if self.content is None else redact_sensitive_text(self.content)

    @property
    def new_handover_content(self) -> str | None:
        """Return what the run hands over (``handover_content``) beyond what it carried over from its pause.

        A reply that a resume continues already accounts for what each resumed run carried over: the text it wrote
        before the pause, or a paused notice that no longer holds once the run continues. A run that was not resumed
        hands over all of it, and a failed run why it failed.
        """
        handover = self.handover_content
        return None if handover == self.carried_content else handover

    def _scalars_dict(self, identity: Mapping[str, str | None]) -> dict[str, Any]:
        """Return the scalar keys in stored order: the run, its agent or team, then the rest."""
        values: dict[str, Any] = {
            "run_id": self.run_id,
            **identity,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "user_id": self.user_id,
            "content": self.content,
            "model": self.model,
            "model_provider": self.model_provider,
        }
        result = {key: value for key, value in values.items() if value is not None}
        result["created_at"] = self.created_at
        return result

    # ``tools``, ``requirements``, and ``metrics`` are written exactly when set, ``[]`` and ``{}`` included, so a
    # stored row writes back as it reads: older rows hold ``tools = []`` on runs without tool calls, and
    # interrupted-turn records carry no ``metrics`` at all.
    def _metrics_dict(self) -> dict[str, Any]:
        return {} if self.metrics is None else {"metrics": self.metrics.to_dict()}

    def _history_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"messages": [message.to_dict() for message in self.messages]}
        if self.metadata is not None:
            result["metadata"] = self.metadata
        return result

    def _tools_dict(self) -> dict[str, Any]:
        return {} if self.tools is None else {"tools": [tool.to_dict() for tool in self.tools]}

    def _requirements_dict(self) -> dict[str, Any]:
        return {} if self.requirements is None else {"requirements": [item.to_dict() for item in self.requirements]}


def _common_from_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    # LEGACY_COMPAT: Agno 3 run dicts carry keys the owned run records dropped.
    # Legacy format: ``run_data`` rows and summary-row ``member_responses`` written by the Agno-backed runtime
    # may hold ``input``, ``content_type``, ``session_state``, ``events``, ``images``/``videos``/``audio``/
    # ``files``, ``response_audio``, ``citations``, ``references``, ``reasoning_*``, ``followups``,
    # ``model_provider_data``, fork and queue lineage fields, and per-tool ``requires_user_input``,
    # ``user_input_schema``, ``user_feedback_schema``, ``answered``, ``approval_id``.
    # Last legacy release: v2026.10.478; replacement: unreleased.
    # Handling: the keys are ignored on read and never written again; every key MindRoom reads survives.
    # Coverage: tests/llm/test_codecs.py::test_run_record_round_trips_agno_dict_minus_dropped_keys,
    #   tests/llm/test_codecs.py::test_stored_fixture_dicts_round_trip_minus_dropped_keys
    tools = data.get("tools")
    requirements = data.get("requirements")
    metrics = data.get("metrics")
    values: dict[str, Any] = {
        "run_id": data["run_id"],
        "session_id": data.get("session_id"),
        "parent_run_id": data.get("parent_run_id"),
        "user_id": data.get("user_id"),
        "status": RunStatus(data.get("status", RunStatus.running.value)),
        "content": data.get("content"),
        "model": data.get("model"),
        "model_provider": data.get("model_provider"),
        "messages": [Message.from_dict(message) for message in data.get("messages") or []],
        "tools": None if tools is None else [ToolExecution.from_dict(tool) for tool in tools],
        "requirements": None if requirements is None else [RunRequirement.from_dict(item) for item in requirements],
        "metrics": None if metrics is None else RunMetrics.from_dict(metrics),
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
        """Return the stored ``run_data`` dict, keys in stored order."""
        return {
            **self._scalars_dict({"agent_id": self.agent_id, "agent_name": self.agent_name}),
            "status": self.status.value,
            **self._metrics_dict(),
            **self._history_dict(),
            **self._tools_dict(),
            **self._requirements_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRecord:
        """Rebuild from a stored ``run_data`` dict, ignoring keys this record does not model."""
        return cls(agent_id=data.get("agent_id"), agent_name=data.get("agent_name"), **_common_from_dict(data))


@dataclass(kw_only=True)
class TeamRunRecord(_RunRecordCommon):
    """A persisted team run; paused member runs ride along in ``member_responses``.

    ``ended_member_runs`` holds the terminal record of every member run that ended while the run ran, in the order
    they first ended: a member run stopped with its team run comes last. A mid-turn fold stores them in its segment,
    and the turn's reply and usage read them. It is never stored, so a loaded record starts without it, while a resume
    of the live record carries it on.
    """

    team_id: str | None = None
    team_name: str | None = None
    member_responses: list[RunRecord] = field(default_factory=list)
    ended_member_runs: list[RunRecord] = field(default_factory=list, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored ``run_data`` dict, keys in stored order; ``member_responses`` is always present."""
        return {
            **self._scalars_dict({"team_id": self.team_id, "team_name": self.team_name}),
            **self._metrics_dict(),
            "status": self.status.value,
            **self._history_dict(),
            "member_responses": [member.to_dict() for member in self.member_responses],
            **self._requirements_dict(),
            **self._tools_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TeamRunRecord:
        """Rebuild from a stored ``run_data`` dict, ignoring keys this record does not model."""
        return cls(
            team_id=data.get("team_id"),
            team_name=data.get("team_name"),
            member_responses=[RunRecord.from_dict(member) for member in data.get("member_responses") or []],
            **_common_from_dict(data),
        )


type AnyRunRecord = RunRecord | TeamRunRecord

__all__ = [
    "AnyRunRecord",
    "RunRecord",
    "RunRequirement",
    "RunStatus",
    "TeamRunRecord",
    "ToolExecution",
    "denial_note_for",
]
