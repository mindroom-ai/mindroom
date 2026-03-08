"""Shared bot runtime data structures and event type aliases."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import nio

from mindroom.constants import ATTACHMENT_IDS_KEY
from mindroom.media_inputs import MediaInputs

if TYPE_CHECKING:
    from mindroom.matrix.identity import MatrixID

__all__ = [
    "_DispatchEvent",
    "_DispatchPayload",
    "_MediaDispatchEvent",
    "_MessageContext",
    "_PreparedDispatch",
    "_ResponseAction",
    "_RouterDispatchResult",
    "_SyntheticTextEvent",
    "_TextDispatchEvent",
]


@dataclass(frozen=True)
class _ResponseAction:
    """Result of the shared team-formation / should-respond decision."""

    kind: Literal["skip", "team", "individual"]
    form_team: Any | None = None


@dataclass(frozen=True)
class _RouterDispatchResult:
    """Whether router dispatch consumed the event and if display-only echoes count as handled."""

    handled: bool
    mark_visible_echo_responded: bool = False


@dataclass(frozen=True)
class _MessageContext:
    """Conversation context derived from reply/thread state."""

    am_i_mentioned: bool
    is_thread: bool
    thread_id: str | None
    thread_history: list[dict[str, Any]]
    mentioned_agents: list[MatrixID]
    has_non_agent_mentions: bool


@dataclass(frozen=True)
class _PreparedDispatch:
    """Prepared dispatch input after common event gating."""

    requester_user_id: str
    context: _MessageContext


@dataclass(frozen=True)
class _DispatchPayload:
    """Prompt plus multimodal payload resolved for a dispatch."""

    prompt: str
    media: MediaInputs = field(default_factory=MediaInputs)
    attachment_ids: list[str] | None = None


@dataclass(frozen=True)
class _SyntheticTextEvent:
    """Synthetic text event produced from normalized non-text input."""

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]


type _TextDispatchEvent = nio.RoomMessageText | _SyntheticTextEvent
type _MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
    | nio.RoomMessageAudio
    | nio.RoomEncryptedAudio
)
type _DispatchEvent = _TextDispatchEvent | _MediaDispatchEvent


def _merge_response_extra_content(
    extra_content: dict[str, Any] | None,
    attachment_ids: list[str] | None,
) -> dict[str, Any] | None:
    """Merge optional attachment IDs into response metadata."""
    merged_extra_content = extra_content if extra_content is not None else {}
    if attachment_ids:
        merged_extra_content[ATTACHMENT_IDS_KEY] = attachment_ids
    return merged_extra_content or None
