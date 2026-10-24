"""Events a run yields while it executes; ``RunFinished`` is always the last one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ReasoningDelta, TextDelta

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics


@dataclass(frozen=True, slots=True)
class ReportedCall:
    """One tool call as its tool event reports it, fixed when the event is made; a run's record keeps the call itself."""

    tool_call_id: str | None
    tool_name: str | None
    tool_args: dict[str, Any] | None = None
    result: str | None = None
    is_error: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolCallOrigin:
    """Where a hidden CLI call ran: the Bash call that ran it and its toolkit. Only the reply's tool trace keeps it."""

    parent_bash_call_id: str | None
    toolkit_name: str | None


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """A tool call was admitted; paused calls are announced too. ``origin`` is set for a hidden CLI call."""

    call: ReportedCall
    origin: ToolCallOrigin | None = None


@dataclass(frozen=True, slots=True)
class ToolCompleted:
    """A tool call finished. ``origin`` is set for a hidden CLI call."""

    call: ReportedCall
    origin: ToolCallOrigin | None = None


@dataclass(frozen=True, slots=True)
class RequestCompleted:
    """One provider request finished with the usage it reported."""

    model_id: str
    provider: str
    usage: MessageMetrics


@dataclass(frozen=True, slots=True)
class RunFinished:
    """The run reached ``COMPLETED`` or ``PAUSED``; the record carries content, tools, and requirements."""

    record: AnyRunRecord


type RunEvent = TextDelta | ReasoningDelta | ToolStarted | ToolCompleted | RequestCompleted | RunFinished

__all__ = [
    "ReasoningDelta",
    "ReportedCall",
    "RequestCompleted",
    "RunEvent",
    "RunFinished",
    "TextDelta",
    "ToolCallOrigin",
    "ToolCompleted",
    "ToolStarted",
]
