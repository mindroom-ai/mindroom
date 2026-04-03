"""Canonical Matrix message-target metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.scheduling import ScheduledWorkflow


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

    @classmethod
    def for_scheduled_task(
        cls,
        workflow: ScheduledWorkflow,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
    ) -> MessageTarget:
        """Resolve the delivery target for one scheduled workflow execution."""
        if workflow.room_id is None:
            msg = "Scheduled workflows require room_id to resolve a MessageTarget"
            raise ValueError(msg)

        room_mode = workflow.new_thread or (
            config.get_entity_thread_mode(
                ROUTER_AGENT_NAME,
                runtime_paths,
                room_id=workflow.room_id,
            )
            == "room"
        )
        return cls.resolve(
            room_id=workflow.room_id,
            thread_id=workflow.thread_id,
            reply_to_event_id=None,
            room_mode=room_mode,
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
            session_id=self.room_id if resolved_thread_id is None else f"{self.room_id}:{resolved_thread_id}",
        )

    @classmethod
    def resolve(
        cls,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None,
        safe_thread_root: str | None = None,
        room_mode: bool = False,
    ) -> MessageTarget:
        """Resolve one canonical delivery target."""
        resolved_thread_id = None if room_mode else thread_id or safe_thread_root or reply_to_event_id

        return cls(
            room_id=room_id,
            thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            session_id=room_id if resolved_thread_id is None else f"{room_id}:{resolved_thread_id}",
        )
