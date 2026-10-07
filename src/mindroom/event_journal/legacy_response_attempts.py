"""One-time adoption of approval continuation identity from released stores."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, cast

from mindroom.handled_turns import TurnRecordCodec

from . import journal
from .models import SUPERSEDED_FAILURE_REASON

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.turn_record import TurnRecord

    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

# LEGACY_COMPAT: Approval continuations whose reply identity lived outside the continuation.
# Legacy format: approval_continuations without a span_id column. Every release kept a continuation's entity in its
# entity_name column and its pending sources in approval_continuation_sources; v2026.10.201 kept its entity, room, visible event, logical and discovery sources, and
# edit receipt order in response_attempts and response_attempt_sources, keyed by its first pending source;
# v2026.9.137 and earlier kept room_id, response_event_id, and any prepared edit record in context_json.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages name the paused span in
# approval_continuations.span_id and read the reply's identity and held sources from the reply's records.
# Handling: the schema upgrade adds span_id, copies each continuation's identity and pending sources into its context
# once, and drops approval_continuation_sources, the entity_name column, and the response attempt tables; such a
# continuation is read from that copy until reply classification names its span.
# Coverage: tests/test_legacy_continuation_identity.py.

_IDENTITY_KEY = "legacy_identity"


class _LegacyIdentity(TypedDict):
    """The reply identity a continuation adopted from an earlier release answers."""

    entity_name: str
    room_id: str
    thread_id: str | None
    response_event_id: str
    pending_event_ids: tuple[str, ...]
    logical_source_event_ids: tuple[str, ...]
    discovery_event_ids: tuple[str, ...]
    edit_receipt_order: int | None
    show_tool_calls: bool
    prepared_edit_record: TurnRecord | None


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
    """Read the identity v2026.10.201 stored in its response attempt tables."""
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


# LEGACY_COMPAT: Approvals whose reply a newer edit's answer already replaced.
# Legacy format: a continuation whose response attempt a newer attempt of the same reply, room, membership, entity, and
# logical sources superseded with a higher edit receipt order and an acknowledged answer FINAL editing the same event,
# while its own FINAL holds no answer; v2026.10.201 retired such an approval's failure without a note.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages fence an approval superseded
# when the edit's regeneration claims its reply.
# Handling: the upgrade fences it superseded, so reply classification adopts the reply the newer answer shows and
# keeps the approval's pause on it until its cleanup settles the sources it holds.
# Coverage: tests/test_legacy_continuation_identity.py::test_an_approval_a_newer_answer_replaced_is_superseded.
def _replaced(transaction: Transaction, principal_id: str, driving: str) -> bool:
    """Return whether a newer edit's acknowledged answer replaced the reply this attempt paused."""
    row = transaction.fetchone(
        """SELECT 1 AS present FROM response_attempts AS attempt
        JOIN response_attempts AS newer
          ON newer.principal_id = attempt.principal_id AND newer.room_id = attempt.room_id
         AND newer.membership_epoch = attempt.membership_epoch
         AND newer.response_event_id = attempt.response_event_id AND newer.entity_name = attempt.entity_name
         AND newer.logical_source_key = attempt.logical_source_key
         AND newer.edit_receipt_order > COALESCE(NULLIF(attempt.edit_receipt_order, 0), attempt.selected_receipt_order)
        JOIN matrix_delivery_outbox AS delivery
          ON delivery.principal_id = newer.principal_id AND delivery.delivery_id = newer.driving_event_id
         AND delivery.room_id = newer.room_id AND delivery.membership_epoch = newer.membership_epoch
         AND delivery.edits_event_id = newer.response_event_id
        WHERE attempt.principal_id = ? AND attempt.driving_event_id = ? AND delivery.stage = 'final'
          AND delivery.acknowledged_event_id IS NOT NULL AND delivery.result_json IS NOT NULL
          AND delivery.retired = 0 AND delivery.permanent_failure_reason IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM matrix_delivery_outbox AS own
            WHERE own.principal_id = attempt.principal_id AND own.delivery_id = attempt.driving_event_id
              AND own.stage = 'final' AND own.result_json IS NOT NULL
          )
        LIMIT 1""",
        (principal_id, driving),
    )
    return row is not None


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
    edit_receipt_order = stored.get("latest_edit_receipt_order")
    if (
        prepared is None
        or not isinstance(edit_receipt_order, int)
        or isinstance(edit_receipt_order, bool)
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
        "edit_receipt_order": edit_receipt_order,
    }


def upgrade_continuation_identity(
    transaction: Transaction,
    existing_tables: frozenset[str],
    continuation_columns: frozenset[str],
) -> None:
    """Name the paused span on continuations and adopt released identities, inside the schema transaction."""
    # No columns: no approval_continuations table yet, or one a pre-Nio-1 upgrade just retired.
    if not continuation_columns or "span_id" in continuation_columns:
        return
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN span_id TEXT")
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN claim_span_id TEXT")
    attempts = "response_attempts" in existing_tables
    cursor: tuple[str, str] | None = None
    while True:
        rows = (
            transaction.fetchall(
                """SELECT principal_id, approval_id, entity_name, state, context_json FROM approval_continuations
                ORDER BY principal_id, approval_id LIMIT ?""",
                (_PAGE_SIZE,),
            )
            if cursor is None
            else transaction.fetchall(
                """SELECT principal_id, approval_id, entity_name, state, context_json FROM approval_continuations
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
            if attempts and _replaced(transaction, principal_id, pending[0]):
                transaction.execute(
                    """UPDATE approval_continuations SET state = 'failing', failure_reason = ?, runtime_generation = NULL
                    WHERE principal_id = ? AND approval_id = ?""",
                    (SUPERSEDED_FAILURE_REASON, principal_id, approval_id),
                )
            stored[_IDENTITY_KEY] = {
                **identity,
                "thread_id": stored.get("thread_id"),
                "pending_event_ids": pending,
            }
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
    transaction.execute("DROP TABLE IF EXISTS approval_continuation_sources")
    # The paused span's reply names the entity; the scan index on the copy goes with it.
    transaction.execute("DROP INDEX IF EXISTS approval_continuations_owner_scan")
    transaction.execute("ALTER TABLE approval_continuations DROP COLUMN entity_name")
    if attempts:
        transaction.execute("DROP TABLE IF EXISTS response_attempt_sources")
        transaction.execute("DROP TABLE IF EXISTS response_attempts")


def legacy_identity_context(continuation: ApprovalContinuation) -> dict[str, object]:
    """Return the context entries that keep a continuation's adopted identity until its span is named.

    That includes the visibility and selected edit an earlier release kept in
    the context; a named span's reply holds both instead.
    """
    if continuation.span_id is not None:
        return {}
    return {
        "show_tool_calls": continuation.show_tool_calls,
        "prepared_edit_record": (
            None
            if continuation.prepared_edit_record is None
            else TurnRecordCodec._to_ledger_record(continuation.prepared_edit_record)
        ),
        _IDENTITY_KEY: {
            "entity_name": continuation.entity_name,
            "room_id": continuation.room_id,
            "thread_id": continuation.thread_id,
            "response_event_id": continuation.response_event_id,
            "pending_event_ids": list(continuation.source_event_ids),
            "logical_source_event_ids": list(continuation.sources.logical_source_event_ids),
            "discovery_event_ids": list(continuation.sources.discovery_event_ids),
            "edit_receipt_order": continuation.sources.edit_receipt_order,
        },
    }


def legacy_identity(context: Mapping[str, object], *, approval_id: str) -> _LegacyIdentity:
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
        "pending_event_ids": tuple(_event_ids(identity.get("pending_event_ids"))),
        "logical_source_event_ids": tuple(_event_ids(identity.get("logical_source_event_ids"))),
        "discovery_event_ids": tuple(_event_ids(identity.get("discovery_event_ids", []))),
        "edit_receipt_order": cast("int | None", identity.get("edit_receipt_order")),
        "show_tool_calls": context.get("show_tool_calls", True) is not False,
        "prepared_edit_record": _prepared_edit(context.get("prepared_edit_record")),
    }


def adopted_entity_name(context: Mapping[str, object]) -> str | None:
    """Return the entity a continuation was adopted with, or nothing once reply classification named its span."""
    identity = context.get(_IDENTITY_KEY)
    if not isinstance(identity, dict):
        return None
    return _required_text(cast("dict[str, object]", identity).get("entity_name"))


def _prepared_edit(raw: object) -> TurnRecord | None:
    if not isinstance(raw, dict):
        return None
    stored = cast("dict[str, object]", raw)
    return TurnRecordCodec._from_ledger_record(str(stored.get("anchor_event_id")), stored)


# LEGACY_COMPAT: Settling an adopted continuation that reply classification never named a span for.
# Legacy format: an approval_continuations row with no span_id, whose identity the schema upgrade copied into its
# context; it stays so when its entity never starts again, such as an entity removed from the configuration.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages settle a continuation's
# sources through its paused span's SettleSources.
# Handling: no bot of its entity runs to answer it, so only its discard reaches it, and its adopted pending sources
# settle unanswered.
# Coverage: tests/test_legacy_continuation_identity.py::test_an_unclassified_continuation_settles_its_adopted_sources.
def settle_unclassified(transaction: Transaction, principal_id: str, continuation: ApprovalContinuation) -> None:
    """Settle the sources an unclassified continuation adopted, leaving its turn unanswered."""
    journal.settle_many(transaction, principal_id, continuation.source_event_ids)
