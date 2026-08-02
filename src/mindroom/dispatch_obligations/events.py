"""Matrix event codecs and typed callback adapters for durable dispatch."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

import nio
from typing_extensions import TypeIs

from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, MatrixMediaEvent, parse_matrix_media_event_source

from .storage import (
    DispatchCallbackKind,
    DispatchObligation,
    DispatchObligationCorruptionError,
    invite_source_event_id,
)

_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_RECOVERY_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"


class DispatchCallbackResult(StrEnum):
    """One explicit callback outcome visible at the durable boundary."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"
    DEFERRED = "deferred"


def callback_kind_for_source_kind(source_kind: str) -> DispatchCallbackKind:
    """Return the durable callback owner for one coalescing source kind."""
    if source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND}:
        return DispatchCallbackKind.MEDIA
    return DispatchCallbackKind.MESSAGE


class _RoomIdEvent(Protocol):
    """Nio event carrying the room attached by its decryption pipeline."""

    room_id: str


DispatchEvent = nio.Event | nio.InviteEvent
DispatchCallback = Callable[[nio.MatrixRoom, DispatchEvent], Awaitable[DispatchCallbackResult]]
MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[TurnDispatchOutcome]]
MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[TurnDispatchOutcome]]
ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
InviteCallback = Callable[[nio.MatrixRoom, nio.InviteEvent], Awaitable[None]]
RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]


def dispatch_event_source(event: DispatchEvent) -> dict[str, object]:
    """Return the stable replay source for one durable callback."""
    source = dict(event.source)
    source.pop(_RECOVERY_SECURITY_METADATA_KEY, None)
    if isinstance(event, nio.Event) and event.decrypted:
        source[_RECOVERY_SECURITY_METADATA_KEY] = {
            "decrypted": True,
            "verified": event.verified,
            "sender_key": event.sender_key,
            "session_id": event.session_id,
        }
    if isinstance(event, nio.InviteMemberEvent):
        # nio pops content while parsing invites, so restore it for stable durable replay keys.
        source["content"] = dict(event.content)
    return source


def _apply_recovery_security_metadata(
    event: nio.Event,
    metadata: object,
    *,
    room_id: str,
    source_event_id: str,
) -> None:
    """Restore nio facts attached after a decrypted payload was parsed."""
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        msg = f"corrupt dispatch obligation event {source_event_id!r}/security metadata"
        raise DispatchObligationCorruptionError(msg)
    metadata_dict = cast("dict[str, object]", metadata)
    verified = metadata_dict.get("verified")
    sender_key = metadata_dict.get("sender_key")
    session_id = metadata_dict.get("session_id")
    if (
        metadata_dict.get("decrypted") is not True
        or not isinstance(verified, bool)
        or (sender_key is not None and not isinstance(sender_key, str))
        or (session_id is not None and not isinstance(session_id, str))
    ):
        msg = f"corrupt dispatch obligation event {source_event_id!r}/security metadata"
        raise DispatchObligationCorruptionError(msg)
    event.decrypted = True
    event.verified = verified
    event.sender_key = sender_key
    event.session_id = session_id
    # Nio attaches this attribute to decrypted events even though its base Event
    # annotation omits it; the protocol keeps that runtime contract explicit here.
    cast(_RoomIdEvent, event).room_id = room_id  # noqa: TC006


def dispatch_source_event_id(
    room_id: str,
    event: DispatchEvent,
    callback_kind: DispatchCallbackKind,
    event_source_json: str,
) -> str:
    """Return the exact durable source identity for one callback."""
    if callback_kind is DispatchCallbackKind.INVITE:
        if not isinstance(event, nio.InviteEvent):
            msg = "Invite dispatch requires an invite event"
            raise TypeError(msg)
        return invite_source_event_id(room_id, event_source_json)
    if not isinstance(event, nio.Event):
        msg = f"{callback_kind.value} dispatch requires an event with an exact Matrix event ID"
        raise TypeError(msg)
    return event.event_id


def parse_recovery_event(obligation: DispatchObligation) -> DispatchEvent:
    """Rebuild one typed nio event from its persisted callback source."""
    if obligation.callback_kind is DispatchCallbackKind.INVITE:
        event_source_json = json.dumps(
            obligation.event_source,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if invite_source_event_id(obligation.room_id, event_source_json) != obligation.source_event_id:
            msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/'invite'"
            raise DispatchObligationCorruptionError(msg)
        event = nio.InviteEvent.parse_event(dict(obligation.event_source))
        if not isinstance(event, nio.InviteEvent):
            msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/'invite'"
            raise DispatchObligationCorruptionError(msg)
        return event
    event_source = dict(obligation.event_source)
    security_metadata = event_source.pop(_RECOVERY_SECURITY_METADATA_KEY, None)
    event = (
        parse_matrix_media_event_source(event_source)
        if obligation.callback_kind is DispatchCallbackKind.MEDIA
        else nio.Event.parse_event(event_source)
    )
    if not isinstance(event, nio.Event) or event.event_id != obligation.source_event_id:
        msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/{obligation.callback_kind.value!r}"
        raise DispatchObligationCorruptionError(msg)
    if isinstance(event, nio.MegolmEvent):
        event.room_id = obligation.room_id
    _apply_recovery_security_metadata(
        event,
        security_metadata,
        room_id=obligation.room_id,
        source_event_id=obligation.source_event_id,
    )
    return event


@dataclass(frozen=True, slots=True)
class CallbackBindings:
    """Bind typed Matrix callbacks to durable callback outcomes."""

    on_message: MessageCallback
    on_media: MediaCallback
    on_reaction: ReactionCallback
    on_approval: ApprovalCallback
    on_invite: InviteCallback
    on_room_lifecycle: RoomLifecycleCallback
    on_redaction: RedactionCallback
    on_decryption_failure: DecryptionFailureCallback
    source_has_live_owner: Callable[[str], bool]

    def as_mapping(self) -> Mapping[DispatchCallbackKind, DispatchCallback]:
        """Return callback bindings keyed by their durable callback kind."""
        return {
            DispatchCallbackKind.MESSAGE: self._dispatch_message,
            DispatchCallbackKind.MEDIA: self._dispatch_media,
            DispatchCallbackKind.REACTION: self._dispatch_reaction,
            DispatchCallbackKind.APPROVAL: self._dispatch_approval,
            DispatchCallbackKind.INVITE: self._dispatch_invite,
            DispatchCallbackKind.ROOM_LIFECYCLE: self._dispatch_room_lifecycle,
            DispatchCallbackKind.REDACTION: self._dispatch_redaction,
            DispatchCallbackKind.DECRYPTION_FAILURE: self._dispatch_decryption_failure,
        }

    @staticmethod
    def _turn_result(outcome: TurnDispatchOutcome) -> DispatchCallbackResult:
        if outcome is TurnDispatchOutcome.DEFERRED:
            return DispatchCallbackResult.DEFERRED
        if outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED:
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        msg = f"Turn dispatch callback returned invalid outcome {outcome!r}"
        raise TypeError(msg)

    async def _dispatch_message(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.RoomMessageText):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        return self._turn_result(await self.on_message(room, event))

    async def _dispatch_media(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, MATRIX_MEDIA_EVENT_TYPES):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        if self.source_has_live_owner(event.event_id):
            return DispatchCallbackResult.DEFERRED
        return self._turn_result(await self.on_media(room, event))

    async def _dispatch_reaction(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.ReactionEvent):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_reaction(room, event)
        return DispatchCallbackResult.SUCCEEDED

    async def _dispatch_approval(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.Event) or not _is_tool_approval_response(event):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_approval(room, event)
        return DispatchCallbackResult.SUCCEEDED

    async def _dispatch_invite(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.InviteEvent):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_invite(room, event)
        return DispatchCallbackResult.SUCCEEDED

    async def _dispatch_room_lifecycle(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.RoomMemberEvent):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_room_lifecycle(room, event)
        return DispatchCallbackResult.SUCCEEDED

    async def _dispatch_redaction(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.RedactionEvent):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_redaction(room, event)
        return DispatchCallbackResult.SUCCEEDED

    async def _dispatch_decryption_failure(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent,
    ) -> DispatchCallbackResult:
        if not isinstance(event, nio.MegolmEvent):
            return DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_decryption_failure(room, event)
        return DispatchCallbackResult.SUCCEEDED


def _is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    return isinstance(event, nio.UnknownEvent) and event.type == _TOOL_APPROVAL_RESPONSE_EVENT_TYPE


@dataclass(frozen=True, slots=True)
class SourceCallbackPolicy:
    """One shared timeline admission and callback-registration rule."""

    callback_kind: DispatchCallbackKind
    event_types: tuple[type[nio.Event], ...]
    predicate: Callable[[nio.Event], bool] | None = None

    def matches(self, event: nio.Event) -> bool:
        """Return whether this policy owns one exact event."""
        return isinstance(event, self.event_types) and (self.predicate is None or self.predicate(event))


SOURCE_CALLBACK_POLICIES = (
    SourceCallbackPolicy(DispatchCallbackKind.MESSAGE, (nio.RoomMessageText,)),
    SourceCallbackPolicy(DispatchCallbackKind.REDACTION, (nio.RedactionEvent,)),
    SourceCallbackPolicy(DispatchCallbackKind.REACTION, (nio.ReactionEvent,)),
    SourceCallbackPolicy(DispatchCallbackKind.MEDIA, MATRIX_MEDIA_EVENT_TYPES),
    SourceCallbackPolicy(
        DispatchCallbackKind.APPROVAL,
        (nio.UnknownEvent,),
        _is_tool_approval_response,
    ),
    SourceCallbackPolicy(DispatchCallbackKind.DECRYPTION_FAILURE, (nio.MegolmEvent,)),
)
