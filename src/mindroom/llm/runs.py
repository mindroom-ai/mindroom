# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Run records, tool executions, and pause requirements, with their stored dict shapes.

The conversation store splits a record's dict across its run, message, and tool rows (``session_storage/codec.py``).
Keys and ``RunStatus`` strings are also those delegation state holds and those of the Agno-layout run rows, which only
the storage v2 conversion still reads; keys the records do not model are ignored on read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from time import time
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from mindroom.llm.events import ReportedCall
from mindroom.llm.legacy_run_content import legacy_failure_text, stored_content
from mindroom.llm.messages import Message
from mindroom.llm.usage import RunMetrics


def _new_id() -> str:
    return str(uuid4())


def _stored_call_id(stored: Mapping[str, Any]) -> str:
    """Return the call id of an entry stored without one, derived from what it stores so every read agrees."""
    return str(uuid5(NAMESPACE_URL, json.dumps(["mindroom-stored-tool-call", dict(stored)], sort_keys=True)))


def _now() -> int:
    return int(time())


class RunStatus(StrEnum):
    """Lifecycle state of a run; the values are the stored ``status`` strings.

    The members have lower-case names because ``.name`` is persisted too (``mindroom_original_status`` in run metadata
    holds ``"cancelled"``, not ``"CANCELLED"``).
    """

    running = "RUNNING"
    completed = "COMPLETED"
    paused = "PAUSED"
    cancelled = "CANCELLED"
    error = "ERROR"


# LEGACY_COMPAT: Run rows stored with Agno's PENDING or REGENERATED status.
# Legacy format: a stored run ``status`` of ``PENDING``, which Agno 3.0.9's ``Agent`` and ``Team`` gave a run started
# with ``arun(background=True)`` or continued with ``acontinue_run(background=True)`` until it ran, or
# ``REGENERATED``, which their ``continue_run(regenerate=True)`` gave the run a regenerated one replaced, and which
# Agno's history skipped.
# Last legacy release: none; no MindRoom release wrote either, since MindRoom never started or continued an Agno run
# in the background or continued one with ``regenerate=True``, and v2026.10.499 through v2026.10.627 modeled both only
# to read such a row. A stored one is unversioned external input.
# Handling: either status reads as ``ERROR``, a run that did not finish, which never replays as history and settles a
# delegation as failed; every other status reads as stored.
# Coverage: tests/llm/test_codecs.py::test_run_record_reads_every_status_agno_could_store,
#   tests/session_storage/test_codec_rows.py::test_a_row_stored_with_an_agno_only_status_decodes_as_a_run_that_did_not_finish
_UNFINISHED_AGNO_STATUSES = frozenset({"PENDING", "REGENERATED"})


def _stored_status(value: str) -> RunStatus:
    return RunStatus.error if value in _UNFINISHED_AGNO_STATUSES else RunStatus(value)


# The stored keys of the tool-execution fields whose names differ from them.
_STORED_TOOL_KEYS = {"is_error": "tool_call_error", "stop_after_call": "stop_after_tool_call"}


@dataclass(kw_only=True)
class ToolExecution:
    """One tool call the model requested, with its outcome or pause state."""

    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any] | None = None
    is_error: bool | None = None
    result: str | None = None
    child_run_id: str | None = None
    stop_after_call: bool = False
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

    @property
    def call(self) -> ReportedCall:
        """The call as a tool event reports it now: id, name, arguments, and any result."""
        return ReportedCall(self.tool_call_id, self.tool_name, self.tool_args, self.result, self.is_error)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict: every field, None included, under its stored key."""
        # TODO(simplify): every field is stored, None defaults included. Store only the fields that hold a value,
        # with the storage redesign (#3107, #3095). Stored data would change: new rows would omit unset fields.
        return {_STORED_TOOL_KEYS.get(name, name): value for name, value in asdict(self).items()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ToolExecution:
        """Rebuild from a stored dict, ignoring keys this type does not model; an absent key keeps its default.

        A call stored without an id reads with one derived from what it stores, the same on every read.
        """
        # LEGACY_COMPAT: Tool executions and run requirements carry Agno's per-call timing and timestamps.
        # Legacy format: stored tool executions hold ``metrics`` (``start_time``, ``end_time``, ``duration``) and
        # ``created_at``, and stored requirements hold an ISO 8601 ``created_at``, in run rows, summary-row segments,
        # delegation state, and CLI approval payloads; no MindRoom code read them.
        # Last legacy release: v2026.10.542, which wrote them as every earlier release did; replacement: v2026.10.543,
        # whose tool executions and requirements store neither.
        # Handling: the keys are ignored on read and never written again, so a rewritten row drops them.
        # Coverage: tests/llm/test_codecs.py::test_tool_execution_round_trips_agno_dict_minus_dropped_keys,
        #   tests/llm/test_codecs.py::test_stored_fixture_dicts_round_trip_minus_dropped_keys
        values = {_TOOL_FIELDS[key]: value for key, value in data.items() if key in _TOOL_FIELDS}
        # LEGACY_COMPAT: Tool executions stored without a call id.
        # Legacy format: a stored tool execution whose ``tool_call_id`` is null, empty, or absent. Agno 2.4.7 stored
        # Ollama's calls so, since Ollama sends no call ids; the ``unknown`` placeholder that ``RunRequirement``
        # reads for a requirement stored without its tool execution was written back so too.
        # Last legacy release: v2026.4.35, the last release on Agno 2.4.7; v2026.4.36 moved to Agno 2.5.13, which
        # gives an id-less call a uuid4 id. For the ``unknown`` placeholder, v2026.10.513, whose typed model wrote it
        # back with a null id; the next release writes its derived id. The requirements it stands in for, stored
        # without a tool execution, are unversioned external input.
        # Handling: the execution reads with an id derived from its stored keys and values, so every read of its
        # row gives the same id and its readers need no missing-id case; two entries storing exactly the same keys
        # and values share it. A row written back stores that id, and every other key reads as stored.
        # Coverage: tests/llm/test_codecs.py::test_a_tool_execution_stored_without_a_call_id_reads_with_the_same_id,
        #   tests/llm/test_codecs.py::test_a_run_whose_tool_executions_have_no_call_ids_still_loads,
        #   tests/llm/test_codecs.py::test_run_requirement_reads_rows_agno_read_leniently
        if not values.get("tool_call_id"):
            values["tool_call_id"] = _stored_call_id(data)
        return cls(**values)


# Each tool-execution field by its stored key.
_TOOL_FIELDS = {_STORED_TOOL_KEYS.get(item.name, item.name): item.name for item in fields(ToolExecution)}

_DENIED_WITHOUT_REASON = "Not approved by requester"


def denial_note_for(reason: str | None) -> str:
    """Return the note the model is told for a denied call: the requester's reason, or that it was not approved."""
    return reason or _DENIED_WITHOUT_REASON


@dataclass
class RunRequirement:
    """What a paused run waits for: a confirmation or an external execution result for one tool call."""

    tool_execution: ToolExecution
    id: str = field(default_factory=_new_id, kw_only=True)
    confirmation: bool | None = field(default=None, kw_only=True)
    confirmation_note: str | None = field(default=None, kw_only=True)
    external_execution_result: str | None = field(default=None, kw_only=True)
    member_agent_id: str | None = field(default=None, kw_only=True)
    member_display_name: str | None = field(default=None, kw_only=True)
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
        """Return the stored dict; None values are dropped."""
        values = {
            "id": self.id,
            "confirmation": self.confirmation,
            "confirmation_note": self.confirmation_note,
            "external_execution_result": self.external_execution_result,
            "member_agent_id": self.member_agent_id,
            "member_agent_name": self.member_display_name,
            "member_run_id": self.member_run_id,
            "tool_execution": self.tool_execution.to_dict(),
        }
        return {key: value for key, value in values.items() if value is not None}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRequirement:
        """Rebuild from a stored dict, ignoring keys this type does not model, such as older rows' user-input keys.

        A missing tool execution becomes an ``unknown`` placeholder with a call id derived from the entry, and a
        decision recorded only on the requirement reaches its tool execution, which is what a resume runs from.
        """
        # LEGACY_COMPAT: Run requirements stored without a tool execution.
        # Legacy format: ``requirements`` entries lacking ``tool_execution``, which Agno's reader accepted as legacy
        # data.
        # Last legacy release: unversioned external input; no MindRoom release is known to write them, and
        # agno 3.0.9 always wrote one.
        # Handling: the entry reads with an ``unknown`` placeholder tool execution, under a call id derived from the
        # entry's stored keys, as Agno's reader did apart from the id, so one such entry never fails the load of its
        # session and every read gives its placeholder the same id.
        # Coverage: tests/llm/test_codecs.py::test_run_requirement_reads_rows_agno_read_leniently
        tool_data = data.get("tool_execution")
        requirement = cls(
            ToolExecution.from_dict(tool_data)
            if isinstance(tool_data, Mapping)
            else ToolExecution(tool_call_id=_stored_call_id(data), tool_name="unknown", tool_args={}),
            id=identifier if isinstance(identifier := data.get("id"), str) else _new_id(),
            confirmation=data.get("confirmation"),
            confirmation_note=data.get("confirmation_note"),
            external_execution_result=data.get("external_execution_result"),
            member_agent_id=data.get("member_agent_id"),
            member_display_name=data.get("member_agent_name"),
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


@dataclass(kw_only=True)
class _RunRecordCommon:
    """Fields shared by agent and team run records."""

    run_id: str
    session_id: str | None = None
    parent_run_id: str | None = None
    user_id: str | None = None
    status: RunStatus = RunStatus.running
    # What the run's model wrote, before a pause and after each resume, its requests' texts a blank line apart
    # (``joined_texts``). A pause is in ``requirements`` and a failure in ``error``, never here.
    content: str | None = None
    # Why the run failed or was stopped, credentials redacted, set by whatever ended it so: the loop for an ``ERROR``
    # run, and delegation recovery for a child run it fails or cancels. A run its own cancellation stopped has none.
    error: str | None = None
    model: str | None = None
    model_provider: str | None = None
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolExecution] | None = None
    # How many of ``tools`` are refused calls that do not count toward the run's tool-call limit: a call naming no
    # offered function, one whose arguments do not decode, and one of a cut-off response. A resume counts the rest.
    uncounted_tool_calls: int = 0
    requirements: list[RunRequirement] | None = None
    metrics: RunMetrics | None = None
    metadata: dict[str, Any] | None = None
    created_at: int = field(default_factory=_now)
    # The content a resumed run carried from its pause, set by the resume (``resumed_record``); the loop joins what the
    # model writes since to it. Never stored, so a loaded record has none.
    carried_content: str | None = field(default=None, compare=False, repr=False)

    @property
    def handover_content(self) -> str | None:
        """Return what the run shows whoever reads it after it ends: why it failed or was stopped, else its content.

        The run that started it, a team's document of its members, and a delegation's settlement all read this.
        """
        if self.error is not None:
            return self.error
        return legacy_failure_text(self.content) if self.status is RunStatus.error else self.content

    @property
    def new_handover_content(self) -> str | None:
        """Return what the run hands over (``handover_content``) beyond the text it carried from its pause.

        The reply a resume continues already shows that text, so it adds only this: what the model wrote since the
        resume, or why the run failed or was stopped. A run that was not resumed hands over all of it.
        """
        handover = self.handover_content
        carried = (self.carried_content or "").strip()
        if self.error is not None or not carried or handover is None:
            return handover
        if handover == self.carried_content:
            return None
        return handover.removeprefix(carried + _TEXT_SEPARATOR)

    def _scalars_dict(self, identity: Mapping[str, str | None]) -> dict[str, Any]:
        """Return the scalar keys in stored order: the run, its agent or team, then the rest."""
        values: dict[str, Any] = {
            "run_id": self.run_id,
            **identity,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "user_id": self.user_id,
            "content": self.content,
            "error": self.error,
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
        tools = {} if self.tools is None else {"tools": [tool.to_dict() for tool in self.tools]}
        # Written only when some call does not count, so a run whose every call counts stores no key.
        return tools | ({"uncounted_tool_calls": self.uncounted_tool_calls} if self.uncounted_tool_calls else {})

    def _requirements_dict(self) -> dict[str, Any]:
        return {} if self.requirements is None else {"requirements": [item.to_dict() for item in self.requirements]}


_TEXT_SEPARATOR = "\n\n"


def joined_texts(parts: Sequence[str]) -> str:
    """Return a run's texts as its content: one text as it is, several stripped and a blank line apart.

    A resumed run's content joins its carried text and the texts of its requests since, so the text before and after
    a tool call or an approval reads as two paragraphs.
    """
    texts = [part for part in parts if part.strip()]
    if len(texts) <= 1:
        return texts[0] if texts else "".join(parts)
    return _TEXT_SEPARATOR.join(text.strip() for text in texts)


def _common_from_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    tools = data.get("tools")
    requirements = data.get("requirements")
    metrics = data.get("metrics")
    values: dict[str, Any] = {
        "run_id": data["run_id"],
        "session_id": data.get("session_id"),
        "parent_run_id": data.get("parent_run_id"),
        "user_id": data.get("user_id"),
        "status": _stored_status(data.get("status", RunStatus.running.value)),
        "content": stored_content(data),
        "error": data.get("error"),
        "model": data.get("model"),
        "model_provider": data.get("model_provider"),
        "messages": [Message.from_dict(message) for message in data.get("messages") or []],
        "tools": None if tools is None else [ToolExecution.from_dict(tool) for tool in tools],
        "uncounted_tool_calls": data.get("uncounted_tool_calls", 0),
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
    agent_display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict, keys in stored order, which the store splits across its run, message, and tool rows."""
        return {
            **self._scalars_dict({"agent_id": self.agent_id, "agent_name": self.agent_display_name}),
            "status": self.status.value,
            **self._metrics_dict(),
            **self._history_dict(),
            **self._tools_dict(),
            **self._requirements_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunRecord:
        """Rebuild from a stored dict, or an Agno-layout ``run_data`` the conversion reads, ignoring unmodeled keys."""
        return cls(agent_id=data.get("agent_id"), agent_display_name=data.get("agent_name"), **_common_from_dict(data))


@dataclass(kw_only=True)
class TeamRunRecord(_RunRecordCommon):
    """A persisted team run; paused member runs ride along in ``member_responses``.

    ``ended_member_runs`` holds the terminal record of every member run that ended while the run ran, in the order
    they first ended: a member run stopped with its team run comes last. A mid-turn fold flags their rows as folded,
    and the turn's reply and usage read them. It is never stored, so a loaded record starts without it, while a resume
    of the live record carries it on.
    """

    team_id: str | None
    member_responses: list[RunRecord] = field(default_factory=list)
    ended_member_runs: list[RunRecord] = field(default_factory=list, compare=False, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Return the stored dict, keys in stored order, split as a run's is; ``member_responses`` is always present."""
        return {
            **self._scalars_dict({"team_id": self.team_id}),
            **self._metrics_dict(),
            "status": self.status.value,
            **self._history_dict(),
            "member_responses": [member.to_dict() for member in self.member_responses],
            **self._requirements_dict(),
            **self._tools_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TeamRunRecord:
        """Rebuild from a stored dict, or an Agno-layout ``run_data`` the conversion reads, ignoring unmodeled keys."""
        # LEGACY_COMPAT: Team run dicts carry the ``team_name`` label of their members.
        # Legacy format: team ``run_data`` rows hold ``team_name``, ``Team-`` followed by the requested member names
        # joined by ``-``; no MindRoom code read it.
        # Last legacy release: v2026.10.506, which wrote it as every earlier release did; replacement: v2026.10.507,
        # whose team runs store no ``team_name``.
        # Handling: the key is ignored on read and never written again, so a rewritten row drops it.
        # Coverage: tests/llm/test_codecs.py::test_team_run_record_round_trips_agno_dict_minus_dropped_keys,
        #   tests/llm/test_codecs.py::test_stored_fixture_dicts_round_trip_minus_dropped_keys
        return cls(
            team_id=data.get("team_id"),
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
    "joined_texts",
]
