"""Canonical Matrix message-target metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict, cast

from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    from mindroom.scheduling import ScheduledWorkflow
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class MessageTargetMetadata(TypedDict):
    """JSON-safe persisted conversation-target identity."""

    room_id: str
    source_thread_id: str | None
    resolved_thread_id: str | None
    reply_to_event_id: str | None
    session_id: str


@dataclass(frozen=True)
class MessageTarget:
    """Single source of truth for where one message should be delivered."""

    room_id: str
    source_thread_id: str | None
    resolved_thread_id: str | None
    reply_to_event_id: str | None
    session_id: str

    def __post_init__(self) -> None:
        """Validate the canonical delivery target identity."""
        if not self.room_id:
            message = "MessageTarget requires a non-empty room_id"
            raise ValueError(message)
        if not self.session_id:
            message = "MessageTarget requires a non-empty session_id"
            raise ValueError(message)
        optional_event_ids = (
            ("source_thread_id", self.source_thread_id),
            ("resolved_thread_id", self.resolved_thread_id),
            ("reply_to_event_id", self.reply_to_event_id),
        )
        for field_name, field_value in optional_event_ids:
            if field_value == "":
                message = f"MessageTarget {field_name} must be None or a non-empty event ID"
                raise ValueError(message)

    @property
    def is_room_mode(self) -> bool:
        """Return whether the target resolves to room-level delivery."""
        return self.resolved_thread_id is None

    @property
    def log_context(self) -> dict[str, str | None]:
        """Return the canonical room/thread log fields for this target."""
        return {"room_id": self.room_id, "thread_id": self.resolved_thread_id}

    _build_session_id = staticmethod(create_session_id)

    def to_metadata(self) -> MessageTargetMetadata:
        """Return JSON-safe conversation-target metadata."""
        return {
            "room_id": self.room_id,
            "source_thread_id": self.source_thread_id,
            "resolved_thread_id": self.resolved_thread_id,
            "reply_to_event_id": self.reply_to_event_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_metadata(cls, raw_metadata: object) -> MessageTarget | None:
        """Return normalized conversation-target metadata."""
        if not isinstance(raw_metadata, Mapping):
            return None
        metadata = cast("Mapping[str, object]", raw_metadata)
        room_id = _metadata_required_string(metadata, "room_id")
        session_id = _metadata_required_string(metadata, "session_id")
        optional_event_ids = _metadata_optional_event_ids(metadata)
        if room_id is None or session_id is None or optional_event_ids is None:
            return None
        source_thread_id, resolved_thread_id, reply_to_event_id = optional_event_ids
        return cls(
            room_id=room_id,
            source_thread_id=source_thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            session_id=session_id,
        )

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
            source_thread_id=context.thread_id,
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
            source_thread_id=self.source_thread_id,
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
        effective_thread_id = None if room_mode else thread_id
        effective_thread_start_root_event_id = None if room_mode else thread_start_root_event_id
        resolved_thread_id = effective_thread_id or effective_thread_start_root_event_id

        return cls(
            room_id=room_id,
            source_thread_id=effective_thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            session_id=cls._build_session_id(room_id, resolved_thread_id),
        )


def _metadata_required_string(raw_metadata: Mapping[str, object], key: str) -> str | None:
    """Return one required non-empty string metadata field."""
    raw_value = raw_metadata.get(key)
    return raw_value if isinstance(raw_value, str) and raw_value else None


def _metadata_optional_event_ids(
    raw_metadata: Mapping[str, object],
) -> tuple[str | None, str | None, str | None] | None:
    """Return the optional event IDs required by serialized target metadata."""
    values: list[str | None] = []
    for key in ("source_thread_id", "resolved_thread_id", "reply_to_event_id"):
        if key not in raw_metadata:
            return None
        raw_value = raw_metadata[key]
        if raw_value is None:
            values.append(None)
        elif isinstance(raw_value, str) and raw_value:
            values.append(raw_value)
        else:
            return None
    return cast("tuple[str | None, str | None, str | None]", tuple(values))
