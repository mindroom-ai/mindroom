"""A redacted, capped snapshot of what one sub-agent run has done so far."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from mindroom.delegation.state import child_execution_identity
from mindroom.hooks.context import SubagentItem, SubagentSnapshot
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.events import format_tool_completed_event, format_tool_started_event

if TYPE_CHECKING:
    from mindroom.delegation.state import DelegationChild
    from mindroom.hooks.context import SubagentStatus
    from mindroom.llm.events import ToolCompleted, ToolStarted
    from mindroom.tool_system.events import ToolTraceEntry

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied"})
_MAX_TEXT_CHARACTERS = 16_000
_MAX_TOOL_ITEMS = 50
_MAX_LABEL_CHARACTERS = 2_000


def _clip(text: str) -> str:
    return redact_sensitive_text(text)[:_MAX_LABEL_CHARACTERS]


@dataclass(slots=True)
class SubagentActivity:
    """The transcript and status of one child run, turned into snapshots a hook can publish."""

    delegation_id: str
    subagent_id: str
    parent_delegation_id: str | None
    caller_agent_name: str
    agent_name: str
    source: Literal["delegate", "workflow"]
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
    def start(cls, child: DelegationChild, *, parent_delegation_id: str | None, now: float) -> SubagentActivity:
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
            task=child.task,
            room_id=identity.room_id or "",
            thread_id=identity.resolved_thread_id or identity.thread_id,
            requester_id=identity.requester_id or "",
            model_name=child.model_name,
            started_at=now,
            updated_at=now,
        )

    def add_text(self, text: str, now: float) -> None:
        """Append the child's words, merging into the last item when that is text, then enforce the text cap."""
        if not text:
            return
        self.updated_at = now
        if self._items and self._items[-1].kind == "text":
            self._items[-1] = replace(self._items[-1], text=self._items[-1].text + text)
        else:
            self._items.append(SubagentItem(kind="text", text=text))
        self._trim_text()

    def _trim_text(self) -> None:
        excess = sum(len(item.text) for item in self._items if item.kind == "text") - _MAX_TEXT_CHARACTERS
        index = 0
        while excess > 0 and index < len(self._items):
            item = self._items[index]
            if item.kind != "text":
                index += 1
                continue
            if len(item.text) > excess:
                self._items[index] = replace(item, text=item.text[excess:])
                return
            excess -= len(item.text)
            del self._items[index]
            self.items_dropped += 1

    def tool_started(self, event: ToolStarted, now: float) -> None:
        """Add a running tool row, dropping the oldest tool row past the cap."""
        self.updated_at = now
        _, entry = format_tool_started_event(event.call, origin=event.origin)
        self._add_tool(event.call.tool_call_id, replace(entry, call=None), "running")

    def tool_completed(self, event: ToolCompleted, now: float) -> None:
        """Finish the row with the same call id, or append a finished row when none matches."""
        self.updated_at = now
        entry = replace(format_tool_completed_event(event.call, origin=event.origin), call=None)
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
        self.ended_at = now if status in TERMINAL_STATUSES else None
        if result is not None:
            self.result = result

    def snapshot(self) -> SubagentSnapshot:
        """Build the redacted, capped snapshot a plugin sees."""
        items = tuple(
            replace(item, text=redact_sensitive_text(item.text)) if item.kind == "text" else item
            for item in self._items
        )
        return SubagentSnapshot(
            delegation_id=self.delegation_id,
            subagent_id=self.subagent_id,
            parent_delegation_id=self.parent_delegation_id,
            caller_agent_name=self.caller_agent_name,
            agent_name=self.agent_name,
            source=self.source,
            task=_clip(self.task),
            room_id=self.room_id,
            thread_id=self.thread_id,
            requester_id=self.requester_id,
            model_name=self.model_name,
            status=self.status,
            started_at=self.started_at,
            updated_at=self.updated_at,
            ended_at=self.ended_at,
            items=items,
            items_dropped=self.items_dropped,
            result_preview=None if self.result is None else _clip(self.result),
        )
