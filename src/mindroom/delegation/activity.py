"""A redacted, capped snapshot of what one sub-agent run has done so far, and its coalesced publication to hooks."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from mindroom.background_tasks import create_background_task
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, child_execution_identity
from mindroom.hooks.context import SubagentChangedContext, SubagentItem, SubagentSnapshot
from mindroom.hooks.execution import emit, has_eligible_hooks
from mindroom.hooks.types import EVENT_SUBAGENT_CHANGED
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.events import ToolTraceEntry, format_tool_completed_event, format_tool_started_event

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.hooks.context import HookBindings, SubagentStatus
    from mindroom.hooks.registry import HookRegistry
    from mindroom.llm.events import ToolCompleted, ToolStarted

logger = get_logger(__name__)

_FLUSH_INTERVAL_SECONDS = 0.25
_MAX_TEXT_CHARACTERS = 16_000
_MAX_TOOL_ITEMS = 50
_MAX_LABEL_CHARACTERS = 2_000
# How much raw text a cap keeps past its limit, so redaction sees a secret whole before the limit cuts the redacted text.
_REDACTION_MARGIN = 4_000


def _clip(text: str) -> str:
    """Return the redacted head of ``text``, at most 2,000 characters; only a head with a margin past that is redacted."""
    return redact_sensitive_text(text[: _MAX_LABEL_CHARACTERS + _REDACTION_MARGIN])[:_MAX_LABEL_CHARACTERS]


def _cut_oldest_text(items: list[SubagentItem], limit: int) -> int:
    """Cut the oldest text in place until the items' text totals at most ``limit``; return how many items it removed."""
    excess = sum(len(item.text) for item in items if item.kind == "text") - limit
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
    """Return a new entry with only the names and redacted previews a shown tool row carries, as `serialize_tool_trace`."""
    return ToolTraceEntry(
        type=entry.type,
        tool_name=entry.tool_name,
        args_preview=None if entry.args_preview is None else redact_sensitive_text(entry.args_preview),
        result_preview=None if entry.result_preview is None else redact_sensitive_text(entry.result_preview),
        truncated=entry.truncated,
    )


@dataclass(slots=True)
class _SubagentActivity:
    """The transcript and status of one child run, turned into snapshots a hook can publish."""

    delegation_id: str
    subagent_id: str
    parent_delegation_id: str | None
    caller_agent_name: str
    agent_name: str
    source: Literal["delegate", "workflow"]
    # Redacted and capped once, at the start.
    task: str
    room_id: str
    thread_id: str | None
    requester_id: str
    model_name: str
    started_at: float
    updated_at: float
    status: SubagentStatus = "running"
    ended_at: float | None = None
    result: str | None = None
    items_dropped: int = 0
    _items: list[SubagentItem] = field(default_factory=list)

    @classmethod
    def start(cls, child: DelegationChild, *, parent_delegation_id: str | None, now: float) -> _SubagentActivity:
        """Begin a running activity for one child."""
        identity = child_execution_identity(child)
        is_workflow = child.persona is not None and child.persona.source_kind == "workflow"
        return cls(
            delegation_id=child.delegation_id,
            subagent_id=child.subagent_id,
            parent_delegation_id=parent_delegation_id,
            caller_agent_name=child.caller_agent_name,
            agent_name=child.child_agent_name,
            source="workflow" if is_workflow else "delegate",
            task=_clip(child.task),
            room_id=identity.room_id or "",
            thread_id=identity.resolved_thread_id or identity.thread_id,
            requester_id=identity.requester_id or "",
            model_name=child.model_name,
            started_at=now,
            updated_at=now,
        )

    def add_text(self, text: str, now: float) -> None:
        """Append the child's raw words, merging into the last item when that is text, keeping the newest raw text.

        The raw text keeps a margin past the text cap, which snapshots apply to the redacted text.
        """
        if not text:
            return
        self.updated_at = now
        if self._items and self._items[-1].kind == "text":
            self._items[-1] = replace(self._items[-1], text=self._items[-1].text + text)
        else:
            self._items.append(SubagentItem(kind="text", text=text))
        self.items_dropped += _cut_oldest_text(self._items, _MAX_TEXT_CHARACTERS + _REDACTION_MARGIN)

    def tool_started(self, event: ToolStarted, now: float) -> None:
        """Add a running tool row, dropping the oldest tool row past the cap."""
        self.updated_at = now
        _, entry = format_tool_started_event(event.call)
        self._add_tool(event.call.tool_call_id, _public_entry(entry), "running")

    def tool_completed(self, event: ToolCompleted, now: float) -> None:
        """Finish the row with the same call id, or append a finished row when none matches."""
        self.updated_at = now
        entry = _public_entry(format_tool_completed_event(event.call))
        state: Literal["done", "error"] = "error" if event.call.is_error else "done"
        for index in range(len(self._items) - 1, -1, -1):
            item = self._items[index]
            if item.kind == "tool" and item.tool_call_id == event.call.tool_call_id:
                self._items[index] = replace(item, tool=entry, state=state)
                return
        self._add_tool(event.call.tool_call_id, entry, state)

    def _add_tool(self, tool_call_id: str, entry: ToolTraceEntry, state: Literal["running", "done", "error"]) -> None:
        self._items.append(SubagentItem(kind="tool", tool=entry, tool_call_id=tool_call_id, state=state))
        tools = [index for index, item in enumerate(self._items) if item.kind == "tool"]
        if len(tools) > _MAX_TOOL_ITEMS:
            del self._items[tools[0]]
            self.items_dropped += 1

    def set_status(self, status: SubagentStatus, now: float, *, result: str | None = None) -> None:
        """Move to a status; terminal ones set `ended_at`, `running` clears it."""
        self.status = status
        self.updated_at = now
        self.ended_at = now if status in TERMINAL_CHILD_STATUSES else None
        if result is not None:
            self.result = result

    def snapshot(self) -> SubagentSnapshot:
        """Build the redacted, capped snapshot a plugin sees: each whole text item is redacted, then the text cap cuts."""
        items = [
            replace(item, text=redact_sensitive_text(item.text)) if item.kind == "text" else item
            for item in self._items
        ]
        dropped = _cut_oldest_text(items, _MAX_TEXT_CHARACTERS)
        return SubagentSnapshot(
            delegation_id=self.delegation_id,
            subagent_id=self.subagent_id,
            parent_delegation_id=self.parent_delegation_id,
            caller_agent_name=self.caller_agent_name,
            agent_name=self.agent_name,
            source=self.source,
            task=self.task,
            room_id=self.room_id,
            thread_id=self.thread_id,
            requester_id=self.requester_id,
            model_name=self.model_name,
            status=self.status,
            started_at=self.started_at,
            updated_at=self.updated_at,
            ended_at=self.ended_at,
            items=tuple(items),
            items_dropped=self.items_dropped + dropped,
            result_preview=None if self.result is None else _clip(self.result),
        )


# The feed of each child that may still resume, such as one paused for an approval; a terminal status drops it.
_OPEN: dict[str, ActivityFeed] = {}


class ActivityFeed:
    """Publish one child's snapshots to `subagent:changed` hooks without ever delaying or failing the child.

    Changes only update the activity and mark it dirty. One sender task per feed emits the latest snapshot at most once
    per 250 ms window, or at once after a status change, with one emission in flight at a time. A child that pauses keeps
    its feed, so every attempt of one delegation publishes through one sender, in order.
    """

    def __init__(self, activity: _SubagentActivity, *, registry: HookRegistry, context_kwargs: dict[str, Any]) -> None:
        self._activity = activity
        self._registry = registry
        self._context_kwargs = context_kwargs
        self._dirty = False
        self._wake = asyncio.Event()
        self._sender: asyncio.Task[None] | None = None

    @classmethod
    def open(
        cls,
        child: DelegationChild,
        *,
        parent_delegation_id: str | None,
        bindings: HookBindings,
        config: Config,
        runtime_paths: RuntimePaths,
        correlation_id: str,
    ) -> ActivityFeed | None:
        """Publish a running child, resuming the feed a paused attempt left; None when no hook would see it."""
        registry = bindings.registry
        feed = _OPEN.pop(child.delegation_id, None)
        if not registry.has_hooks(EVENT_SUBAGENT_CHANGED):
            return None
        context_kwargs = bindings.context_kwargs(
            EVENT_SUBAGENT_CHANGED,
            config=config,
            runtime_paths=runtime_paths,
            correlation_id=correlation_id,
        )
        activity = (
            _SubagentActivity.start(child, parent_delegation_id=parent_delegation_id, now=time.time())
            if feed is None
            else feed._activity
        )
        # Scoped hooks see a child by its caller and room, which no later attempt changes.
        context = SubagentChangedContext(**context_kwargs, subagent=activity.snapshot())
        if not has_eligible_hooks(registry, EVENT_SUBAGENT_CHANGED, context):
            return None
        if feed is None:
            feed = cls(activity, registry=registry, context_kwargs=context_kwargs)
        else:
            # The resumed attempt's hooks and turn publish from now on.
            feed._registry, feed._context_kwargs = registry, context_kwargs
        _OPEN[child.delegation_id] = feed
        feed.status("running")
        return feed

    @staticmethod
    def find(delegation_id: str) -> ActivityFeed | None:
        """Return the feed of a child that has not ended, such as one waiting for its approvals."""
        return _OPEN.get(delegation_id)

    def text(self, text: str) -> None:
        """Add the child's words; they go out with the next window's snapshot."""
        self._activity.add_text(text, time.time())
        self._mark()

    def tool_started(self, event: ToolStarted) -> None:
        """Add a running tool row; it goes out with the next window's snapshot."""
        self._activity.tool_started(event, time.time())
        self._mark()

    def tool_completed(self, event: ToolCompleted) -> None:
        """Finish a tool row; it goes out with the next window's snapshot."""
        self._activity.tool_completed(event, time.time())
        self._mark()

    def status(self, status: SubagentStatus, *, result: str | None = None) -> None:
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
                context = SubagentChangedContext(**self._context_kwargs, subagent=self._activity.snapshot())
                await emit(self._registry, EVENT_SUBAGENT_CHANGED, context)
            except Exception:
                logger.exception("Sub-agent activity emission failed", delegation_id=self._activity.delegation_id)
