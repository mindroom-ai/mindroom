"""One-time adoption of approval continuation identity from released stores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from mindroom.handled_turns import TurnRecordCodec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

# LEGACY_COMPAT: Approval continuations whose reply identity lived outside the continuation.
# Legacy format: approval_continuations without a span_id column. v2026.10.178 kept each continuation's entity, room,
# visible event, logical and discovery sources, and edit receipt order in response_attempts and
# response_attempt_sources, keyed by its first pending source; v2026.9.137 and earlier kept room_id,
# response_event_id, and any prepared edit record in context_json.
# Last legacy release: v2026.10.178; replacement: the unreleased durable reply messages name the paused span in
# approval_continuations.span_id and read the reply's identity from the reply's records.
# Handling: the schema upgrade adds span_id, copies each continuation's identity into its context once, and drops the
# response attempt tables; such a continuation is read from that copy until reply classification names its span.
# Coverage: tests/test_legacy_continuation_identity.py.

_IDENTITY_KEY = "legacy_identity"


class LegacyIdentity(TypedDict):
    """The reply identity a continuation adopted from an earlier release answers."""

    entity_name: str
    room_id: str
    thread_id: str | None
    response_event_id: str
    logical_source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...]
    edit_receipt_order: int | None


_PAGE_SIZE = 128


def _identity_error() -> ValueError:
    """Fail required ownership instead of fabricating or settling a live continuation."""
    return ValueError("Cannot migrate required approval continuation identity")


def _required_text(value: object) -> str:
    """Validate a required historical identity field."""
    if not isinstance(value, str) or not value:
        raise _identity_error()
    return value


def _event_ids(value: object) -> list[str]:
    """Validate literal historical source arrays before the tolerant turn decoder."""
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _identity_error()
    return list(cast("list[str]", value))


def _attempt_identity(transaction: Transaction, principal_id: str, driving: str) -> dict[str, object] | None:
    """Read the identity v2026.10.178 stored in its response attempt tables."""
    attempt = transaction.fetchone(
        "SELECT * FROM response_attempts WHERE principal_id = ? AND driving_event_id = ?",
        (principal_id, driving),
    )
    if attempt is None or attempt["response_event_id"] is None:
        return None
    children = transaction.fetchall(
        """SELECT event_id, source_kind FROM response_attempt_sources
        WHERE principal_id = ? AND driving_event_id = ? ORDER BY source_kind, source_ordinal""",
        (principal_id, driving),
    )
    return {
        "entity_name": str(attempt["entity_name"]),
        "room_id": str(attempt["room_id"]),
        "response_event_id": str(attempt["response_event_id"]),
        "logical_source_event_ids": [str(child["event_id"]) for child in children if child["source_kind"] == "logical"],
        "discovery_event_ids": [str(child["event_id"]) for child in children if child["source_kind"] == "discovery"],
        "edit_receipt_order": None if attempt["edit_receipt_order"] is None else int(attempt["edit_receipt_order"]),
    }


def _context_identity(entity_name: str, pending: list[str], context: Mapping[str, object]) -> dict[str, object]:
    """Read the identity v2026.9.137 and earlier kept in the continuation's context."""
    room_id = _required_text(context.get("room_id"))
    response_event_id = _required_text(context.get("response_event_id"))
    raw = context.get("prepared_edit_record")
    if raw is None:
        return {
            "entity_name": entity_name,
            "room_id": room_id,
            "response_event_id": response_event_id,
            "logical_source_event_ids": pending,
            "discovery_event_ids": [],
            "edit_receipt_order": None,
        }
    if not isinstance(raw, dict):
        raise _identity_error()
    stored = cast("dict[str, object]", raw)
    prepared = TurnRecordCodec._from_ledger_record(str(stored.get("anchor_event_id", "")), stored)
    if (
        prepared is None
        or prepared.latest_edit_receipt_order is None
        or pending[0] not in {revision[1] for revision in (prepared.source_event_revisions or {}).values()}
        or prepared.response_owner != entity_name
        or prepared.response_event_id != response_event_id
        or prepared.conversation_target is None
        or prepared.conversation_target.room_id != room_id
    ):
        raise _identity_error()
    return {
        "entity_name": entity_name,
        "room_id": room_id,
        "response_event_id": response_event_id,
        "logical_source_event_ids": _event_ids(stored.get("source_event_ids")),
        "discovery_event_ids": _event_ids(stored.get("discovery_event_ids", [])),
        "edit_receipt_order": prepared.latest_edit_receipt_order,
    }


def upgrade_continuation_identity(
    transaction: Transaction,
    existing_tables: frozenset[str],
    continuation_columns: frozenset[str],
) -> None:
    """Name the paused span on continuations and adopt released identities, inside the schema transaction."""
    if "approval_continuations" not in existing_tables or "span_id" in continuation_columns:
        return
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN span_id TEXT")
    attempts = "response_attempts" in existing_tables
    cursor: tuple[str, str] | None = None
    while True:
        rows = (
            transaction.fetchall(
                """SELECT principal_id, approval_id, entity_name, context_json FROM approval_continuations
                ORDER BY principal_id, approval_id LIMIT ?""",
                (_PAGE_SIZE,),
            )
            if cursor is None
            else transaction.fetchall(
                """SELECT principal_id, approval_id, entity_name, context_json FROM approval_continuations
                WHERE (principal_id, approval_id) > (?, ?)
                ORDER BY principal_id, approval_id LIMIT ?""",
                (*cursor, _PAGE_SIZE),
            )
        )
        for row in rows:
            principal_id, approval_id = str(row["principal_id"]), str(row["approval_id"])
            context = json.loads(str(row["context_json"]))
            if not isinstance(context, dict):
                raise _identity_error()
            pending = [
                str(source["event_id"])
                for source in transaction.fetchall(
                    """SELECT event_id FROM approval_continuation_sources
                    WHERE principal_id = ? AND approval_id = ? ORDER BY source_ordinal""",
                    (principal_id, approval_id),
                )
            ]
            if not pending:
                raise _identity_error()
            stored = cast("dict[str, object]", context)
            identity = (attempts and _attempt_identity(transaction, principal_id, pending[0])) or _context_identity(
                _required_text(row["entity_name"]),
                pending,
                stored,
            )
            stored[_IDENTITY_KEY] = {**identity, "thread_id": stored.get("thread_id")}
            transaction.execute(
                "UPDATE approval_continuations SET context_json = ? WHERE principal_id = ? AND approval_id = ?",
                (
                    json.dumps(stored, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                    principal_id,
                    approval_id,
                ),
            )
        if len(rows) < _PAGE_SIZE:
            break
        cursor = str(rows[-1]["principal_id"]), str(rows[-1]["approval_id"])
    if attempts:
        transaction.execute("DROP TABLE IF EXISTS response_attempt_sources")
        transaction.execute("DROP TABLE IF EXISTS response_attempts")


def legacy_identity_context(continuation: ApprovalContinuation) -> dict[str, object]:
    """Return the context entries that keep a continuation's adopted identity until its span is named."""
    if continuation.span_id is not None:
        return {}
    return {
        _IDENTITY_KEY: {
            "entity_name": continuation.entity_name,
            "room_id": continuation.room_id,
            "thread_id": continuation.thread_id,
            "response_event_id": continuation.response_event_id,
            "logical_source_event_ids": list(continuation.sources.logical_source_event_ids),
            "discovery_event_ids": list(continuation.sources.discovery_event_ids),
            "edit_receipt_order": continuation.sources.edit_receipt_order,
        },
    }


def legacy_identity(context: Mapping[str, object], *, approval_id: str) -> LegacyIdentity:
    """Return the identity a continuation was adopted with, until reply classification names its span."""
    raw = context.get(_IDENTITY_KEY)
    if not isinstance(raw, dict):
        message = f"Approval continuation {approval_id!r} names no paused span"
        raise ValueError(message)  # noqa: TRY004 - a corrupt row, not a caller's type
    identity = cast("dict[str, object]", raw)
    return {
        "entity_name": _required_text(identity.get("entity_name")),
        "room_id": _required_text(identity.get("room_id")),
        "thread_id": cast("str | None", identity.get("thread_id")),
        "response_event_id": _required_text(identity.get("response_event_id")),
        "logical_source_event_ids": tuple(_event_ids(identity.get("logical_source_event_ids"))),
        "discovery_event_ids": tuple(_event_ids(identity.get("discovery_event_ids", []))),
        "edit_receipt_order": cast("int | None", identity.get("edit_receipt_order")),
    }
