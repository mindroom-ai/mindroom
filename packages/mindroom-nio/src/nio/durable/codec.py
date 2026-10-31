"""Freeze sync observations and replay them through ordinary event parsers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._json_copy import copy_json
from ..events import (
    AccountDataEvent,
    AuthenticatedToDeviceEvent,
    DummyEvent,
    Event,
    ForwardedRoomKeyEvent,
    InviteEvent,
    InviteMemberEvent,
    KeyVerificationCancel,
    MegolmEvent,
    RoomKeyEvent,
    ToDeviceEvent,
    UnknownToDeviceEvent,
)
from ..responses import SlidingSyncStateStub
from .model import CryptoEvidence, RecordKind, SyncRecord

if TYPE_CHECKING:
    from ..client.base_client import _SyncItem


def freeze_event(item: _SyncItem) -> SyncRecord:
    """Detach mutable input at its yield, before later state or trust changes."""
    if item.route in ("presence", "ephemeral"):
        raise ValueError("transient observations are not durable records")
    event = item.event
    room_id = item.room.room_id if item.room else None
    if event is None:
        return SyncRecord(
            RecordKind.ROOM_LIFECYCLE, room_id, {"membership": item.section}
        )

    event_source = getattr(event, "source", {})
    payload = copy_json(event_source)
    codec = None
    if isinstance(event, SlidingSyncStateStub):
        codec = "state_stub"
        payload = {"type": event.type, "state_key": event.state_key}
    elif isinstance(event, InviteEvent):
        codec = "invite"
        if isinstance(event, InviteMemberEvent):
            payload["content"] = copy_json(event.content)
    elif isinstance(event, ForwardedRoomKeyEvent):
        codec = "forwarded_room_key"
    elif isinstance(event, RoomKeyEvent):
        codec = "room_key"
    elif isinstance(event, DummyEvent):
        codec = "dummy"
    elif isinstance(event, KeyVerificationCancel) and not payload:
        # Local timeouts have no server event to parse on restart.
        payload = {
            "type": "m.key.verification.cancel",
            "sender": event.sender,
            "content": {
                "transaction_id": event.transaction_id,
                "code": event.code,
                "reason": event.reason,
            },
        }

    decrypted = isinstance(event, Event) and event.decrypted
    encrypted_envelope = (
        item.source is not None
        and item.source.get("type") == "m.room.encrypted"
        and (
            event_source.get("type") != "m.room.encrypted"
            or isinstance(event, AuthenticatedToDeviceEvent)
        )
    )
    crypto = None
    if decrypted or isinstance(event, (RoomKeyEvent, DummyEvent)) or encrypted_envelope:
        envelope_content = item.source.get("content", {}) if item.source else {}
        crypto = CryptoEvidence(
            getattr(event, "verified", None),
            getattr(event, "sender_key", None)
            or envelope_content.get("sender_key", ""),
            getattr(event, "session_id", None),
            getattr(event, "authenticated_sender", None),
        )
    # Plaintext observations normally share the parsed event source. Detach
    # that tree only once, without comparing potentially large event bodies.
    source = (
        payload
        if item.source is None or item.source is event_source
        else copy_json(item.source)
    )
    clear = (
        payload
        if decrypted
        or codec in ("room_key", "forwarded_room_key", "dummy")
        or encrypted_envelope
        else None
    )
    if item.route in ("to_device", "expired_verification"):
        kind = RecordKind.TO_DEVICE
    elif item.route == "room_account_data":
        kind = RecordKind.ROOM_ACCOUNT_DATA
    elif item.route == "global_account_data":
        kind = RecordKind.GLOBAL_ACCOUNT_DATA
    elif item.route == "event":
        kind = RecordKind.TIMELINE
    else:
        kind = RecordKind.STATE
    return SyncRecord(
        kind,
        room_id,
        source,
        clear=clear,
        crypto=crypto,
        provenance=getattr(event, "timeline_provenance", None),
        route=item.route,
        codec=codec,
    )


def restore_event(record: SyncRecord) -> object:
    """Reconstruct a committed event without consulting mutable crypto state."""
    payload: dict[str, Any] = copy_json(
        record.clear if record.clear is not None else record.source
    )
    if record.codec in ("room_key", "forwarded_room_key", "dummy"):
        if record.crypto is None:
            raise ValueError("sanitized to-device record requires crypto evidence")
        if record.codec == "dummy":
            return DummyEvent(
                payload,
                payload["sender"],
                record.crypto.sender_key,
                payload["sender_device"],
            )
        cls = (
            ForwardedRoomKeyEvent
            if record.codec == "forwarded_room_key"
            else RoomKeyEvent
        )
        content = payload["content"]
        return cls(
            payload,
            payload["sender"],
            record.crypto.sender_key,
            content["room_id"],
            content["session_id"],
            content["algorithm"],
        )
    if record.codec == "invite":
        return InviteEvent.parse_event(payload)
    if record.codec == "state_stub":
        return SlidingSyncStateStub(payload["type"], payload["state_key"])
    if record.codec is not None:
        raise ValueError(f"unsupported stored event codec: {record.codec}")
    if record.kind == RecordKind.TO_DEVICE:
        identity = record.crypto.authenticated_sender if record.crypto else None
        if identity is not None:
            # Reconstruct the exact wrapper produced by Olm decryption. The
            # ordinary parser can recognize reserved names differently and
            # treats empty content as redacted, neither of which may rewrite
            # authenticated capture history.
            envelope = record.source.get("content")
            sender_keys = payload.get("keys")
            if (
                record.clear is None
                or not isinstance(payload.get("sender"), str)
                or not isinstance(payload.get("type"), str)
                or not isinstance(payload.get("content"), dict)
                or record.source.get("type") != "m.room.encrypted"
                or record.source.get("sender") != identity.user_id
                or not isinstance(envelope, dict)
                or envelope.get("algorithm") != "m.olm.v1.curve25519-aes-sha2"
                or envelope.get("sender_key") != identity.curve25519
                or payload["sender"] != identity.user_id
                # Live Olm authentication permits an omitted sender_device,
                # but requires any supplied ID and signing key to agree.
                or payload.get("sender_device", identity.device_id)
                != identity.device_id
                or not isinstance(sender_keys, dict)
                or sender_keys.get("ed25519") != identity.ed25519
                or record.crypto is None
                or record.crypto.sender_key != identity.curve25519
            ):
                raise ValueError("stored authenticated identity does not match event")
            event = UnknownToDeviceEvent.from_dict(payload)
            return AuthenticatedToDeviceEvent(
                event.source, event.sender, event.type, identity
            )
        return ToDeviceEvent.parse_event(payload)
    if record.kind in (RecordKind.ROOM_ACCOUNT_DATA, RecordKind.GLOBAL_ACCOUNT_DATA):
        return AccountDataEvent.parse_event(payload)
    if record.kind not in (RecordKind.TIMELINE, RecordKind.STATE):
        return payload
    event = (
        Event.parse_decrypted_event(payload)
        if record.clear is not None
        else Event.parse_event(payload)
    )
    if isinstance(event, MegolmEvent) and record.room_id is not None:
        event.room_id = record.room_id
    if record.crypto is not None and isinstance(event, Event):
        event.decrypted = True
        event.verified = record.crypto.verified  # type: ignore[assignment]
        event.sender_key = record.crypto.sender_key
        event.session_id = record.crypto.session_id
    return event
