"""Events a run yields while it executes; ``RunFinished`` is always the last one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.provider import ReasoningDelta

if TYPE_CHECKING:
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.llm.usage import MessageMetrics


@dataclass(frozen=True, slots=True)
class ContentDelta:
    """A piece of the reply text."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolStarted:
    """A tool call was admitted; paused calls are announced too."""

    tool: ToolExecution


@dataclass(frozen=True, slots=True)
class ToolCompleted:
    """A tool call finished."""

    tool: ToolExecution


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


type RunEvent = ContentDelta | ReasoningDelta | ToolStarted | ToolCompleted | RequestCompleted | RunFinished

__all__ = [
    "ContentDelta",
    "ReasoningDelta",
    "RequestCompleted",
    "RunEvent",
    "RunFinished",
    "ToolCompleted",
    "ToolStarted",
]
