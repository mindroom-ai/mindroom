"""A redacted, capped snapshot of what one sub-agent run has done so far, and its coalesced hand-off to a sink."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import create_background_task
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, child_execution_identity
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.events import ToolTraceEntry, format_tool_completed_event, format_tool_started_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.delegation.state import DelegationChild
    from mindroom.llm.events import ToolCompleted, ToolStarted

logger = get_logger(__name__)

_FLUSH_INTERVAL_SECONDS = 0.25
_MAX_TEXT_CHARACTERS = 6_000
# Text past the cap is cut to this, so the text after a cut grows at its end until the next cut, and the frames that
# carry it are appends.
_CUT_TEXT_CHARACTERS = 5_000
_MAX_TOOL_ITEMS = 20
_MAX_TASK_CHARACTERS = 1_000
_MAX_RESULT_CHARACTERS = 1_000
_MAX_PREVIEW_CHARACTERS = 300
# The raw text a cap keeps past its limit, so redaction sees a secret whole before the limit cuts the redacted text.
_REDACTION_MARGIN = 4_000

type _SubagentStatus = Literal["running", "awaiting_approval", "paused", "completed", "failed", "cancelled", "denied"]


@dataclass(frozen=True, slots=True)
class _SubagentItem:
    """One step of a sub-agent's transcript: a stretch of its text or one of its tool calls."""

    kind: Literal["text", "tool"]
    # kind "text": the child's words so far, redacted Markdown.
    text: str = ""
    # kind "tool": the public trace fields a shown tool row carries.
    tool: ToolTraceEntry | None = None
    tool_call_id: str | None = None
    state: Literal["running", "done", "error"] | None = None


@dataclass(frozen=True, slots=True)
class SubagentSnapshot:
    """What one sub-agent run has done so far, redacted and capped."""

    delegation_id: str
    # The sub-agent this run is a turn of: the delegation ID of its first turn, which its later turns share.
    subagent_id: str
    # Set for a sub-agent's own sub-agent.
    parent_delegation_id: str | None
    caller_agent_name: str
    # The agent in the room whose turn started the delegation chain; the caller itself for a direct child.
    root_caller_agent_name: str
    agent_name: str
    source: Literal["delegate", "workflow"]
    # Redacted, at most 1,000 characters.
    task: str
    thread_id: str | None
    status: _SubagentStatus
    started_at: float
    ended_at: float | None
    # Oldest first.
    items: tuple[_SubagentItem, ...]
    # How many older items the transcript caps removed.
    items_dropped: int
    # Redacted, at most 1,000 characters; set only once the run ended.
    result_preview: str | None


type SubagentSink = Callable[[SubagentSnapshot], None]


def _clip(text: str, limit: int) -> str:
    """Return the redacted head of ``text``, at most ``limit`` characters, redacting only a head with a margin past it."""
    return redact_sensitive_text(text[: limit + _REDACTION_MARGIN])[:limit]


def _cut_preview(preview: str | None) -> tuple[str | None, bool]:
    """Return the redacted ``preview`` cut to the entry cap, and whether the cut removed anything."""
    if preview is None:
        return None, False
    redacted = redact_sensitive_text(preview)
    return redacted[:_MAX_PREVIEW_CHARACTERS], len(redacted) > _MAX_PREVIEW_CHARACTERS


def _text_length(items: list[_SubagentItem]) -> int:
    return sum(len(item.text) for item in items if item.kind == "text")


def _cut_oldest_text(items: list[_SubagentItem], limit: int) -> int:
    """Cut the oldest text in place until the items' text totals at most ``limit``; return how many items it removed."""
    excess = _text_length(items) - limit
    removed = 0
    index = 0
    while excess > 0 and index < len(items):
        item = items[index]
        if item.kind != "text":
            index += 1
            continue
        if len(item.text) > excess:
            items[index] = replace(item, text=item.text[excess:])
            break
        excess -= len(item.text)
        del items[index]
        removed += 1
    return removed


def _public_entry(entry: ToolTraceEntry) -> ToolTraceEntry:
    """Return a new entry with only the names and the redacted previews a shown tool trace carries, cut to 300 characters."""
    args_preview, args_cut = _cut_preview(entry.args_preview)
    result_preview, result_cut = _cut_preview(entry.result_preview)
    return ToolTraceEntry(
        type=entry.type,
        tool_name=entry.tool_name,
        args_preview=args_preview,
        result_preview=result_preview,
        truncated=entry.truncated or args_cut or result_cut,
    )


@dataclass(slots=True)
class _SubagentActivity:
    """The transcript and status of one child run, turned into snapshots a sink can take."""

    delegation_id: str
    subagent_id: str
    parent_delegation_id: str | None
    caller_agent_name: str
    root_caller_agent_name: str
    agent_name: str
    source: Literal["delegate", "workflow"]
    # Redacted and capped once, at the start.
    task: str
    thread_id: str | None
    started_at: float
    status: _SubagentStatus = "running"
    ended_at: float | None = None
    result: str | None = None
    items_dropped: int = 0
    _items: list[_SubagentItem] = field(default_factory=list)
    # How many characters of the redacted text the cap hides, kept from one cut to the next; None after the raw text
    # was cut, so the next snapshot cuts afresh.
    _hidden: int | None = 0

    @classmethod
    def start(
        cls,
        child: DelegationChild,
        *,
        parent_delegation_id: str | None,
        root_caller_agent_name: str,
        now: float,
    ) -> _SubagentActivity:
        """Begin a running activity for one child of the chain ``root_caller_agent_name``'s turn started."""
        identity = child_execution_identity(child)
        is_workflow = child.persona is not None and child.persona.source_kind == "workflow"
        return cls(
            delegation_id=child.delegation_id,
            subagent_id=child.subagent_id,
            parent_delegation_id=parent_delegation_id,
            caller_agent_name=child.caller_agent_name,
            root_caller_agent_name=root_caller_agent_name,
            agent_name=child.child_agent_name,
            source="workflow" if is_workflow else "delegate",
            task=_clip(child.task, _MAX_TASK_CHARACTERS),
            thread_id=identity.resolved_thread_id or identity.thread_id,
            started_at=now,
        )

    def add_text(self, text: str) -> None:
        """Append the child's raw words, merging into the last item when that is text, keeping the newest raw text.

        The raw text keeps a margin past the text cap, which snapshots apply to the redacted text.
        """
        if not text:
            return
        if self._items and self._items[-1].kind == "text":
            self._items[-1] = replace(self._items[-1], text=self._items[-1].text + text)
        else:
            self._items.append(_SubagentItem(kind="text", text=text))
        if _text_length(self._items) > _MAX_TEXT_CHARACTERS + _REDACTION_MARGIN:
            self.items_dropped += _cut_oldest_text(self._items, _CUT_TEXT_CHARACTERS + _REDACTION_MARGIN)
            self._hidden = None

    def tool_started(self, event: ToolStarted) -> None:
        """Add a running tool row; a call announced again, as a paused call is when it resumes, runs on in its row."""
        _, entry = format_tool_started_event(event.call)
        self._set_tool(event.call.tool_call_id, _public_entry(entry), "running")

    def tool_completed(self, event: ToolCompleted) -> None:
        """Finish the row with the same call id, or append a finished row when none matches."""
        entry = _public_entry(format_tool_completed_event(event.call))
        self._set_tool(event.call.tool_call_id, entry, "error" if event.call.is_error else "done")

    def _set_tool(self, tool_call_id: str, entry: ToolTraceEntry, state: Literal["running", "done", "error"]) -> None:
        """Update the newest row of the call, else append one, dropping the oldest tool row past the cap."""
        for index in range(len(self._items) - 1, -1, -1):
            item = self._items[index]
            if item.kind == "tool" and item.tool_call_id == tool_call_id:
                self._items[index] = replace(item, tool=entry, state=state)
                return
        self._items.append(_SubagentItem(kind="tool", tool=entry, tool_call_id=tool_call_id, state=state))
        tools = [index for index, item in enumerate(self._items) if item.kind == "tool"]
        if len(tools) > _MAX_TOOL_ITEMS:
            del self._items[tools[0]]
            self.items_dropped += 1

    def set_status(self, status: _SubagentStatus, now: float, *, result: str | None = None) -> None:
        """Move to a status; terminal ones set `ended_at`, `running` clears it."""
        self.status = status
        self.ended_at = now if status in TERMINAL_CHILD_STATUSES else None
        if result is not None:
            self.result = result

    def snapshot(self) -> SubagentSnapshot:
        """Build the redacted, capped snapshot: whole text items are redacted, then the text cap cuts.

        Text past 6,000 characters is cut to its newest 5,000; until the next cut, the text keeps its start.
        """
        items = [
            replace(item, text=redact_sensitive_text(item.text)) if item.kind == "text" else item
            for item in self._items
        ]
        total = _text_length(items)
        if self._hidden is None or total - self._hidden > _MAX_TEXT_CHARACTERS:
            self._hidden = max(0, total - _CUT_TEXT_CHARACTERS)
        dropped = _cut_oldest_text(items, total - self._hidden)
        return SubagentSnapshot(
            delegation_id=self.delegation_id,
            subagent_id=self.subagent_id,
            parent_delegation_id=self.parent_delegation_id,
            caller_agent_name=self.caller_agent_name,
            root_caller_agent_name=self.root_caller_agent_name,
            agent_name=self.agent_name,
            source=self.source,
            task=self.task,
            thread_id=self.thread_id,
            status=self.status,
            started_at=self.started_at,
            ended_at=self.ended_at,
            items=tuple(items),
            items_dropped=self.items_dropped + dropped,
            result_preview=None if self.result is None else _clip(self.result, _MAX_RESULT_CHARACTERS),
        )


# The feed of each child that runs or may still resume, such as one paused for an approval, which the child's updates
# look up; a terminal status drops it.
_OPEN: dict[str, ActivityFeed] = {}


class ActivityFeed:
    """Hand one child's snapshots to a sink without ever delaying or failing the child.

    Changes only update the activity and mark it dirty. One sender task per feed hands the sink the latest snapshot at
    most once per 250 ms window, or at once after a status change. A child that pauses keeps its feed, so every attempt
    of one delegation publishes through one sender, in order.
    """

    def __init__(self, activity: _SubagentActivity, *, sink: SubagentSink) -> None:
        self._activity = activity
        self._sink = sink
        self._dirty = False
        self._wake = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None

    @classmethod
    def open(
        cls,
        child: DelegationChild,
        *,
        parent_delegation_id: str | None,
        root_caller_agent_name: str,
        sink: SubagentSink,
    ) -> ActivityFeed:
        """Publish a running child to ``sink``, resuming the feed a paused attempt left."""
        feed = _OPEN.pop(child.delegation_id, None)
        if feed is None:
            activity = _SubagentActivity.start(
                child,
                parent_delegation_id=parent_delegation_id,
                root_caller_agent_name=root_caller_agent_name,
                now=time.time(),
            )
            feed = cls(activity, sink=sink)
        else:
            # The resumed attempt's sink publishes from now on.
            feed._sink = sink
        _OPEN[child.delegation_id] = feed
        feed.status("running")
        return feed

    @staticmethod
    def find(delegation_id: str) -> ActivityFeed | None:
        """Return the feed of a child that has not ended, such as one waiting for its approvals."""
        return _OPEN.get(delegation_id)

    def text(self, text: str) -> None:
        """Add the child's words; they go out with the next window's snapshot."""
        self._activity.add_text(text)
        self._mark()

    def tool_started(self, event: ToolStarted) -> None:
        """Add a running tool row; it goes out with the next window's snapshot."""
        self._activity.tool_started(event)
        self._mark()

    def tool_completed(self, event: ToolCompleted) -> None:
        """Finish a tool row; it goes out with the next window's snapshot."""
        self._activity.tool_completed(event)
        self._mark()

    def status(self, status: _SubagentStatus, *, result: str | None = None) -> None:
        """Move to a status and publish it at once; a terminal status also forgets the feed."""
        self._activity.set_status(status, time.time(), result=result)
        self._mark(urgent=True)
        if status in TERMINAL_CHILD_STATUSES:
            _OPEN.pop(self._activity.delegation_id, None)

    def _mark(self, *, urgent: bool = False) -> None:
        self._dirty = True
        if urgent:
            self._wake.set()
        if self._sender is None or self._sender.done():
            self._sender = create_background_task(
                self._send(),
                name=f"subagent_activity_{self._activity.delegation_id}",
                # Outside the child's span and context: publishing is no part of the child's turn.
                context=Context(),
            )

    async def _send(self) -> None:
        while self._dirty:
            if not self._wake.is_set():
                with suppress(TimeoutError):
                    async with asyncio.timeout(_FLUSH_INTERVAL_SECONDS):
                        await self._wake.wait()
            self._wake.clear()
            self._dirty = False
            try:
                self._sink(self._activity.snapshot())
            except Exception:
                logger.exception("Sub-agent activity sink failed", delegation_id=self._activity.delegation_id)
