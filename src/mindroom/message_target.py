"""Canonical Matrix message-target metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.scheduling import ScheduledWorkflow
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@dataclass(frozen=True)
class MessageTarget:
    """Single source of truth for where one message should be delivered."""

    room_id: str
    thread_id: str | None
    resolved_thread_id: str | None
    reply_to_event_id: str | None
    session_id: str

    @property
    def is_room_mode(self) -> bool:
        """Return whether the target resolves to room-level delivery."""
        return self.resolved_thread_id is None

    @property
    def log_context(self) -> dict[str, str | None]:
        """Return the canonical room/thread log fields for this target."""
        return {"room_id": self.room_id, "thread_id": self.resolved_thread_id}

    @staticmethod
    def _build_session_id(room_id: str, resolved_thread_id: str | None) -> str:
        """Build the canonical persisted session ID for one target."""
        return room_id if resolved_thread_id is None else f"{room_id}:{resolved_thread_id}"

    @classmethod
    def for_scheduled_task(
        cls,
        workflow: ScheduledWorkflow,
    ) -> MessageTarget:
        """Resolve the delivery target for one scheduled workflow execution."""
        if workflow.room_id is None:
            msg = "Scheduled workflows require room_id to resolve a MessageTarget"
            raise ValueError(msg)

        return cls.resolve(
            room_id=workflow.room_id,
            thread_id=None if workflow.new_thread else workflow.thread_id,
            reply_to_event_id=None,
            room_mode=workflow.new_thread or workflow.thread_id is None,
        )

    @classmethod
    def from_runtime_context(cls, context: ToolRuntimeContext) -> MessageTarget:
        """Build the canonical target represented by one tool runtime context."""
        return cls(
            room_id=context.room_id,
            thread_id=context.thread_id,
            resolved_thread_id=context.resolved_thread_id,
            reply_to_event_id=context.reply_to_event_id,
            session_id=context.session_id or cls._build_session_id(context.room_id, context.resolved_thread_id),
        )

    def with_thread_root(self, resolved_thread_id: str | None) -> MessageTarget:
        """Return a copy with an overridden resolved thread root."""
        if self.resolved_thread_id == resolved_thread_id:
            return self
        return MessageTarget(
            room_id=self.room_id,
            thread_id=self.thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=self.reply_to_event_id,
            session_id=self._build_session_id(self.room_id, resolved_thread_id),
        )

    @classmethod
    def resolve(
        cls,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        thread_start_root_event_id: str | None = None,
        room_mode: bool = False,
    ) -> MessageTarget:
        """Resolve one canonical delivery target."""
        resolved_thread_id = None if room_mode else thread_id or thread_start_root_event_id

        return cls(
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            session_id=cls._build_session_id(room_id, resolved_thread_id),
        )
