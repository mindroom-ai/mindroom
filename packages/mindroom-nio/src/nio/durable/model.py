"""Committed observations at the application admission boundary."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from ..event_provenance import TimelineEventProvenance
from ..events import AuthenticatedDevice


class RecordKind(StrEnum):
    TIMELINE = "timeline"
    STATE = "state"
    ROOM_ACCOUNT_DATA = "room_account_data"
    GLOBAL_ACCOUNT_DATA = "global_account_data"
    TO_DEVICE = "to_device"
    ROOM_LIFECYCLE = "room_lifecycle"
    LOSS = "loss"


@dataclass(frozen=True, slots=True)
class CryptoEvidence:
    verified: bool | None
    sender_key: str
    session_id: str | None = None
    authenticated_sender: AuthenticatedDevice | None = None


@dataclass(frozen=True, slots=True)
class OwnMembership:
    previous: str | None
    current: str
    previous_epoch: int
    current_epoch: int
    source: Literal["local", "reported"] = "reported"


@dataclass(frozen=True, slots=True)
class SyncRecord:
    kind: RecordKind
    room_id: str | None
    source: dict[str, Any]
    clear: dict[str, Any] | None = None
    provenance: TimelineEventProvenance | None = None
    membership_epoch: int | None = None
    crypto: CryptoEvidence | None = None
    membership: OwnMembership | None = None
    route: str | None = None
    codec: str | None = None


@dataclass(frozen=True, slots=True)
class SyncBatch:
    stream_id: UUID
    sequence: int
    records: tuple[SyncRecord, ...]
    completes_sync: bool = False


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def encode_records(records: tuple[SyncRecord, ...]) -> str:
    # Copy only the small metadata, not the potentially large event trees.
    return encode_json([
        {
            "kind": record.kind,
            "room_id": record.room_id,
            "source": record.source,
            "clear": record.clear,
            "provenance": record.provenance,
            "membership_epoch": record.membership_epoch,
            "crypto": asdict(record.crypto) if record.crypto else None,
            "membership": asdict(record.membership) if record.membership else None,
            "route": record.route,
            "codec": record.codec,
        }
        for record in records
    ])


def decode_records(encoded: str) -> tuple[SyncRecord, ...]:
    """Decode the versioned on-disk carrier once, before exposing records."""
    values = json.loads(encoded)
    if not isinstance(values, list):
        raise ValueError("stored batch records must be an array")
    records = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("source"), dict):
            raise ValueError("stored record requires an event object")
        if value.get("clear") is not None and not isinstance(value["clear"], dict):
            raise ValueError("stored clear event must be an object")
        for field in ("room_id", "route", "codec"):
            if value.get(field) is not None and not isinstance(value[field], str):
                raise ValueError(f"stored {field} must be a string")
        value["kind"] = RecordKind(value["kind"])
        if value.get("provenance") is not None:
            value["provenance"] = TimelineEventProvenance(value["provenance"])
        if value.get("crypto") is not None:
            crypto = value["crypto"]
            if not isinstance(crypto, dict):
                raise ValueError("stored crypto evidence must be an object")
            if (
                crypto.get("verified") is not None
                and type(crypto["verified"]) is not bool
            ):
                raise ValueError("stored verification must be boolean or null")
            if not isinstance(crypto.get("sender_key"), str):
                raise ValueError("stored sender key must be a string")
            if crypto.get("session_id") is not None and not isinstance(
                crypto["session_id"], str
            ):
                raise ValueError("stored session ID must be a string or null")
            try:
                identity = crypto.get("authenticated_sender")
                if identity is not None:
                    if not isinstance(identity, dict):
                        raise ValueError(
                            "stored authenticated identity must be an object"
                        )
                    crypto["authenticated_sender"] = AuthenticatedDevice(**identity)
                value["crypto"] = CryptoEvidence(**crypto)
            except TypeError as error:
                raise ValueError("invalid stored crypto evidence") from error
        if value.get("membership_epoch") is not None:
            _validate_epoch(value["membership_epoch"])
        if value.get("membership") is not None:
            membership = value["membership"]
            if not isinstance(membership, dict):
                raise ValueError("stored membership must be an object")
            allowed = ("join", "invite", "leave", "ban", "knock")
            if membership.get("current") not in allowed or membership.get(
                "previous"
            ) not in (*allowed, None):
                raise ValueError("invalid stored membership state")
            if membership.get("source", "reported") not in ("local", "reported"):
                raise ValueError("invalid stored membership source")
            _validate_epoch(membership.get("previous_epoch"))
            _validate_epoch(membership.get("current_epoch"))
            try:
                value["membership"] = OwnMembership(**membership)
            except TypeError as error:
                raise ValueError("invalid stored membership") from error
        records.append(SyncRecord(**value))
    return tuple(records)


def _validate_epoch(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("stored membership epoch must be a nonnegative integer")
