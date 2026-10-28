"""One-time conversion of the approval records an earlier release kept, inside the schema transaction."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from mindroom.logging_config import get_logger

from . import approval_calls, approval_cards, scheduled_approvals
from .approval_calls import JOB_ENDED_REASON, CallKey
from .identity import encode_thread_id
from .schema import APPROVAL_TABLES, create_statement

if TYPE_CHECKING:
    from .backend import Row, Transaction
    from .upgrades import SchemaInspector

logger = get_logger(__name__)

# LEGACY_COMPAT: Approval decisions and cards kept in per-kind tables.
# Legacy format: approval_continuation_calls and background_approval_calls held each call's decision; approval_cards
# named a card's call as (continuation_id, continuation_generation, tool_call_id) with generation -1 for a detached
# run; approval_grant_cards copied a card's grant scope and the grant that applied to it; approval_grants copied the
# scope again with the originating card's event; scheduled_call_approvals held its card's event, decider, and approved
# scope; approval_action_tombstones held the events of retired cards and receipt aliases. Selected when any of those
# tables exists.
# Last legacy release: v2026.10.655; replacement: the next release keeps one approval_calls row per call, one
# approval_cards row per card or receipt, approval_card_events, approval_card_scopes, and trimmed approval_grants and
# scheduled_call_approvals.
# Handling: copies every fact a decision or a resume reads, so a card pending before the upgrade can still be answered
# and its paused reply resumes; metadata never stored is left NULL. Receipts become receipt cards, retired cards
# become retired card rows where a grant or binding names them, and tombstones become card events; a tombstone no
# converted card shows gets a retired card of its own with a placeholder call, so a click on it is terminal. A live
# card or receipt whose paused reply and calls are gone gets its decided call back from its own payloads. Pending
# background-job calls are denied with the reason a restart gives them and their terminal edits are queued.
# Coverage: tests/test_legacy_approval_records.py::test_every_recording_converts_to_consistent_rows_once,
# tests/test_legacy_approval_records.py::test_a_pending_card_can_still_be_approved_and_its_reply_becomes_ready,
# tests/test_legacy_approval_records.py::test_a_scheduled_approval_still_arms_and_claims,
# tests/test_legacy_approval_records.py::test_timed_grants_keep_their_scope_receipts_and_revocation,
# tests/test_legacy_approval_records.py::test_a_retired_card_click_is_still_terminal,
# tests/test_legacy_approval_records.py::test_a_pending_job_card_is_denied_as_a_restart_denies_it,
# tests/test_legacy_approval_records.py::test_a_departed_responders_card_keeps_its_decided_call_until_it_retires,
# tests/test_legacy_approval_records.py::test_a_receipt_outliving_its_reply_keeps_its_granted_call_until_it_retires,
# tests/test_legacy_continuation_identity.py::test_an_approval_an_earlier_release_left_pending_is_cancelled.

_DROPPED_TABLES = (
    "approval_continuation_calls",
    "background_approval_calls",
    "approval_grant_cards",
    "approval_action_tombstones",
)
_REPLACED_TABLES = ("approval_cards", "approval_grants", "scheduled_call_approvals")
_TOMBSTONE_DELIVERY_PREFIX = "legacy-tombstone:"


def upgrade_approval_records(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Convert an earlier release's approval tables to one row per call and card, inside the schema transaction."""
    tables = inspector.tables()
    if not any(table in tables for table in _DROPPED_TABLES):
        return
    legacy = _Legacy.load(transaction, tables)
    for table in (*_DROPPED_TABLES, *_REPLACED_TABLES):
        if table in tables:
            transaction.execute(f"DROP TABLE {table}")
    for statement in APPROVAL_TABLES:
        transaction.execute(create_statement(statement, inspector.dialect))
    converted = _Converted()
    converted.build(legacy)
    converted.insert(transaction)
    for call in converted.pending_jobs:
        approval_calls.resolve_call(transaction, call, requested="denied", reason=JOB_ENDED_REASON, metadata=None)
    logger.warning(
        "legacy_approval_records_converted",
        calls=len(converted.calls),
        cards=len(converted.cards),
        denied_job_calls=len(converted.pending_jobs),
    )


def _ns(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value)
    parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class _Delivery:
    """One delivery's INITIAL and FINAL outbox rows, as the earlier release left them."""

    room_id: str | None = None
    thread_id: str | None = None
    initial: dict[str, Any] | None = None
    initial_event_id: str | None = None
    final: dict[str, Any] | None = None


@dataclass(slots=True)
class _Legacy:
    """Every row of the earlier release's approval tables, with the outbox rows of their deliveries."""

    reply_calls: tuple[Row, ...]
    detached_calls: tuple[Row, ...]
    cards: tuple[Row, ...]
    grant_cards: tuple[Row, ...]
    grants: tuple[Row, ...]
    bindings: tuple[Row, ...]
    tombstones: tuple[Row, ...]
    deliveries: dict[tuple[str, str], _Delivery]
    # The old rows by the call or delivery they name.
    card_for_call: dict[CallKey, Row] = field(init=False)
    grant_card_for_call: dict[CallKey, Row] = field(init=False)
    binding_for_delivery: dict[tuple[str, str], Row] = field(init=False)
    grant_for_delivery: dict[tuple[str, str], Row] = field(init=False)

    def __post_init__(self) -> None:
        self.card_for_call = {_old_call(row): row for row in self.cards}
        self.grant_card_for_call = {_old_call(row): row for row in self.grant_cards if row["grant_id"] is not None}
        self.binding_for_delivery = {(str(row["principal_id"]), str(row["delivery_id"])): row for row in self.bindings}
        self.grant_for_delivery = {
            (str(row["principal_id"]), str(row["original_delivery_id"])): row for row in self.grants
        }

    @classmethod
    def load(cls, transaction: Transaction, tables: frozenset[str]) -> _Legacy:
        def rows(table: str) -> tuple[Row, ...]:
            return transaction.fetchall(f"SELECT * FROM {table}") if table in tables else ()  # noqa: S608

        deliveries: dict[tuple[str, str], _Delivery] = {}
        # A journal from before durable Matrix consumers has just had its outbox retired.
        outbox_rows = (
            transaction.fetchall(
                """
                SELECT principal_id, delivery_id, stage, room_id, thread_id, payload_json, acknowledged_event_id
                FROM matrix_delivery_outbox
                WHERE event_type = 'io.mindroom.tool_approval' AND stage IN ('initial', 'final')
                """,
            )
            if "matrix_delivery_outbox" in tables
            else ()
        )
        for row in outbox_rows:
            key = (str(row["principal_id"]), str(row["delivery_id"]))
            delivery = deliveries.get(key, _Delivery())
            if row["stage"] == "initial":
                delivery = replace(
                    delivery,
                    room_id=str(row["room_id"]),
                    thread_id=str(row["thread_id"]),
                    initial=approval_cards.decode_object_payload(row["payload_json"], description="approval card"),
                    initial_event_id=cast("str | None", row["acknowledged_event_id"]),
                )
            else:
                delivery = replace(
                    delivery,
                    final=approval_cards.decode_object_payload(
                        row["payload_json"],
                        description="approval terminal edit",
                    ),
                )
            deliveries[key] = delivery
        return cls(
            reply_calls=rows("approval_continuation_calls"),
            detached_calls=rows("background_approval_calls"),
            cards=rows("approval_cards"),
            grant_cards=rows("approval_grant_cards"),
            grants=rows("approval_grants"),
            bindings=rows("scheduled_call_approvals"),
            tombstones=rows("approval_action_tombstones"),
            deliveries=deliveries,
        )

    def delivery(self, principal_id: str, delivery_id: str) -> _Delivery:
        return self.deliveries.get((principal_id, delivery_id), _Delivery())


def _scope(row: Row) -> dict[str, object]:
    """Return the scope row of the consent an old grant card or grant row copied, at its card's membership epoch."""
    return {
        "scope_key": row["scope_key"],
        "operation": row["operation"],
        "requester_id": row["requester_id"],
        "entity_name": row["entity_name"],
        "invoking_agent": row["invoking_agent"],
        "responder_principal_id": row["responder_principal_id"],
        "responder_epoch": int(row["responder_epoch"]),
        "consent_epoch": int(row["membership_epoch"]),
    }


def _old_call(row: Row) -> CallKey:
    """Name the call an old card row or grant card row showed; a detached run's cards used generation -1."""
    return CallKey(str(row["continuation_id"]), max(0, int(row["continuation_generation"])), str(row["tool_call_id"]))


@dataclass(slots=True)
class _Converted:
    """The current rows the conversion writes."""

    calls: dict[CallKey, dict[str, object]] = field(default_factory=dict)
    cards: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    events: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    scopes: dict[tuple[str, str], dict[str, object]] = field(default_factory=dict)
    grants: list[dict[str, object]] = field(default_factory=list)
    bindings: list[dict[str, object]] = field(default_factory=list)
    pending_jobs: list[CallKey] = field(default_factory=list)
    # Rebuilt calls whose card never got a terminal edit, which the conversion queues as the timeout would have.
    _final_edits: list[CallKey] = field(default_factory=list)

    def build(self, legacy: _Legacy) -> None:
        # In this order: a later table's rows skip the cards and calls an earlier one converted.
        self._reply_calls(legacy)
        self._detached_calls(legacy)
        self._request_cards(legacy)
        self._grant_cards(legacy)
        self._grants(legacy)
        self._bindings(legacy)
        self._tombstones(legacy)

    def _reply_calls(self, legacy: _Legacy) -> None:
        """Paused-reply calls; approver from the request card, decider from its terminal edit."""
        for row in legacy.reply_calls:
            call = CallKey(str(row["approval_id"]), int(row["generation"]), str(row["tool_call_id"]))
            card = legacy.card_for_call.get(call)
            delivery = (
                _Delivery() if card is None else legacy.delivery(str(card["principal_id"]), str(card["delivery_id"]))
            )
            grant_card = legacy.grant_card_for_call.get(call)
            self.calls[call] = {
                "principal_id": str(row["principal_id"]),
                "owner_kind": "reply",
                "call_ordinal": int(row["call_ordinal"]),
                "tool_name": row["tool_name"],
                "toolkit_name": row["toolkit_name"],
                "invoking_agent": row["invoking_agent"],
                "arguments_digest": row["arguments_digest"],
                "human_approval_required": row["human_approval_required"],
                "approver_user_id": _text((delivery.initial or {}).get("approver_user_id")),
                "expires_at_ns": int(row["expires_at_ns"]),
                "decision": row["decision"],
                "reason": row["reason"],
                "resolved_by": _text((delivery.final or {}).get("resolved_by")),
                "resolved_at_ns": _ns((delivery.final or {}).get("resolved_at")),
                "approved_scope": None,
                "grant_id": None if grant_card is None else grant_card["grant_id"],
            }

    def _detached_calls(self, legacy: _Legacy) -> None:
        """Detached calls: scheduled when a binding names the card, a job's under tool-job:, otherwise a script's."""
        for row in legacy.detached_calls:
            principal_id = str(row["principal_id"])
            delivery_id = str(row["delivery_id"])
            call = CallKey.detached(str(row["run_id"]), str(row["call_id"]))
            binding = legacy.binding_for_delivery.get((principal_id, delivery_id))
            delivery = legacy.delivery(principal_id, delivery_id)
            initial = delivery.initial or {}
            final = delivery.final or {}
            owner_kind = (
                "scheduled"
                if binding is not None
                else "job"
                if call.owner_id.startswith(approval_calls.JOB_RUN_PREFIX)
                else "script"
            )
            grant_card = legacy.grant_card_for_call.get(call)
            resolved_by = _text(final.get("resolved_by"))
            resolved_at_ns = _ns(final.get("resolved_at"))
            approved_scope = None
            if binding is not None:
                resolved_by = cast("str | None", binding["decided_by"])
                resolved_at_ns = cast("int | None", binding["decided_at_ns"])
                approved_scope = _text(final.get("scheduled_scope")) or cast("str | None", binding["approved_scope"])
            self.calls[call] = {
                "principal_id": principal_id,
                "owner_kind": owner_kind,
                "call_ordinal": self._next_ordinal(call),
                "tool_name": binding["tool_name"] if binding is not None else _text(initial.get("tool_name")),
                "toolkit_name": binding["toolkit_name"] if binding is not None else None,
                "invoking_agent": binding["agent_name"] if binding is not None else _text(initial.get("agent_name")),
                "arguments_digest": None,
                "human_approval_required": None,
                "approver_user_id": _text(initial.get("approver_user_id"))
                or (None if binding is None else str(binding["requester_id"])),
                "expires_at_ns": int(row["expires_at_ns"]),
                "decision": row["decision"],
                "reason": row["reason"],
                "resolved_by": resolved_by,
                "resolved_at_ns": resolved_at_ns,
                "approved_scope": approved_scope,
                "grant_id": None if grant_card is None else grant_card["grant_id"],
            }
            if owner_kind == "job" and row["decision"] is None:
                self.pending_jobs.append(call)

    def _request_cards(self, legacy: _Legacy) -> None:
        """Request cards still holding delivery debt."""
        for row in legacy.cards:
            principal_id = str(row["principal_id"])
            delivery_id = str(row["delivery_id"])
            delivery = legacy.delivery(principal_id, delivery_id)
            if delivery.initial is None:
                continue
            call = _old_call(row)
            self._card(
                principal_id,
                delivery_id,
                kind="request",
                call=call,
                room_id=str(delivery.room_id),
                thread_id=str(delivery.thread_id),
                approvable=delivery.initial.get("approvable", True) is True,
                retired=False,
                event_id=delivery.initial_event_id,
            )
            if call not in self.calls:
                # A responder departure deleted the paused reply and kept this card's terminal-edit debt.
                self._rebuilt_call(principal_id, call, delivery, grant_id=None)

    def _grant_cards(self, legacy: _Legacy) -> None:
        """Timed-grant cards: receipts, retired cards a grant names, and the consent a live card offers."""
        for row in legacy.grant_cards:
            principal_id = str(row["principal_id"])
            delivery_id = str(row["delivery_id"])
            call = _old_call(row)
            live_request = (principal_id, delivery_id) in self.cards
            originating = (principal_id, delivery_id) in legacy.grant_for_delivery
            if live_request or originating:
                self.scopes[principal_id, delivery_id] = _scope(row)
            if live_request or row["grant_id"] is None:
                continue
            delivery = legacy.delivery(principal_id, delivery_id)
            if delivery.initial is not None:
                self._card(
                    principal_id,
                    delivery_id,
                    kind="grant_receipt",
                    call=call,
                    room_id=str(delivery.room_id),
                    thread_id=str(delivery.thread_id),
                    approvable=False,
                    retired=False,
                    event_id=delivery.initial_event_id,
                )
                if call not in self.calls:
                    self._rebuilt_call(principal_id, call, delivery, grant_id=str(row["grant_id"]))
                continue
            grant = legacy.grant_for_delivery.get((principal_id, delivery_id))
            self._card(
                principal_id,
                delivery_id,
                kind="request" if originating else "grant_receipt",
                call=call,
                room_id=str(row["room_id"]),
                thread_id=encode_thread_id(str(row["thread_id"])),
                approvable=False,
                retired=True,
                event_id=None if grant is None else str(grant["card_event_id"]),
            )

    def _grants(self, legacy: _Legacy) -> None:
        """Grants point at their originating card, whose consent and event the grant row also kept."""
        for row in legacy.grants:
            principal_id = str(row["principal_id"])
            delivery_id = str(row["original_delivery_id"])
            self.scopes.setdefault((principal_id, delivery_id), _scope(row))
            if (principal_id, delivery_id) not in self.cards:
                # Its timed-grant card row is gone too; a retired card's call is never read.
                self._card(
                    principal_id,
                    delivery_id,
                    kind="request",
                    call=CallKey(delivery_id, 0, delivery_id),
                    room_id=str(row["room_id"]),
                    thread_id=encode_thread_id(str(row["thread_id"])),
                    approvable=False,
                    retired=True,
                    event_id=str(row["card_event_id"]),
                )
            self._event(principal_id, str(row["card_event_id"]), delivery_id=delivery_id)
            self.grants.append(
                {
                    "principal_id": principal_id,
                    "grant_id": row["grant_id"],
                    "delivery_id": delivery_id,
                    "expires_at_ns": int(row["expires_at_ns"]),
                    "revoked_at_ns": row["revoked_at_ns"],
                    "resolution_json": row["resolution_json"],
                },
            )

    def _bindings(self, legacy: _Legacy) -> None:
        """Scheduled bindings, their request cards, and their receipts."""
        for row in legacy.bindings:
            principal_id = str(row["principal_id"])
            task_id = str(row["task_id"])
            call = CallKey.detached(scheduled_approvals.scheduled_call_run_id(task_id), task_id)
            delivery_id = str(row["delivery_id"])
            initial = legacy.delivery(principal_id, delivery_id).initial
            if (principal_id, delivery_id) not in self.cards:
                self._card(
                    principal_id,
                    delivery_id,
                    kind="request",
                    call=call,
                    room_id=str(row["room_id"]),
                    thread_id=encode_thread_id(str(row["thread_id"])),
                    approvable=False,
                    retired=True,
                    event_id=cast("str | None", row["card_event_id"]),
                )
            receipt_id = cast("str | None", row["consumed_delivery_id"])
            if receipt_id is not None:
                receipt = legacy.delivery(principal_id, receipt_id)
                self._card(
                    principal_id,
                    receipt_id,
                    kind="scheduled_receipt",
                    call=call,
                    room_id=str(row["room_id"]),
                    thread_id=encode_thread_id(str(row["thread_id"])),
                    approvable=False,
                    retired=receipt.initial is None,
                    event_id=receipt.initial_event_id,
                )
            self.bindings.append(
                {
                    "principal_id": principal_id,
                    "task_id": task_id,
                    "room_id": row["room_id"],
                    "thread_id": row["thread_id"],
                    "requester_id": row["requester_id"],
                    "agent_name": row["agent_name"],
                    "toolkit_name": row["toolkit_name"],
                    "tool_name": row["tool_name"],
                    "arguments_json": row["arguments_json"],
                    "workflow_digest": row["workflow_digest"],
                    "execute_at_ns": int(row["execute_at_ns"]),
                    "membership_epoch": int(row["membership_epoch"]),
                    "any_arguments_offered": int(
                        initial is not None and initial.get("scheduled_scope_options") is not None,
                    ),
                    "armed_at_ns": row["armed_at_ns"],
                    "armed_scope": None if row["armed_at_ns"] is None else row["approved_scope"],
                    "revoked_at_ns": row["revoked_at_ns"],
                    "consumed_at_ns": row["consumed_at_ns"],
                    "outcome": row["outcome"],
                },
            )

    def _tombstones(self, legacy: _Legacy) -> None:
        """Retired cards and receipt aliases no converted card shows.

        Tombstones never stored their delivery or thread, and a retired card's call is never read.
        """
        for row in legacy.tombstones:
            principal_id = str(row["principal_id"])
            event_id = str(row["card_event_id"])
            if (principal_id, event_id) in self.events:
                continue
            delivery_id = _TOMBSTONE_DELIVERY_PREFIX + event_id
            self._card(
                principal_id,
                delivery_id,
                kind="request",
                call=CallKey(delivery_id, 0, delivery_id),
                room_id=str(row["room_id"]),
                thread_id=encode_thread_id(None),
                approvable=False,
                retired=True,
                event_id=event_id,
            )

    def _card(
        self,
        principal_id: str,
        delivery_id: str,
        *,
        kind: str,
        call: CallKey,
        room_id: str,
        thread_id: str,
        approvable: bool,
        retired: bool,
        event_id: str | None,
    ) -> None:
        self.cards.setdefault(
            (principal_id, delivery_id),
            {
                "kind": kind,
                "owner_id": call.owner_id,
                "generation": call.generation,
                "tool_call_id": call.tool_call_id,
                "room_id": room_id,
                "thread_id": thread_id,
                "approvable": int(approvable),
                "retired": int(retired),
            },
        )
        if event_id is not None:
            self._event(principal_id, event_id, delivery_id=delivery_id)

    def _event(self, principal_id: str, event_id: str, *, delivery_id: str) -> None:
        self.events.setdefault((principal_id, event_id), {"delivery_id": delivery_id})

    def _rebuilt_call(self, principal_id: str, call: CallKey, delivery: _Delivery, *, grant_id: str | None) -> None:
        """Rebuild a decided paused-reply call from the payloads of the card that still shows it."""
        initial = delivery.initial or {}
        final = delivery.final
        if grant_id is not None:
            decision, reason, decided = "approved", None, initial
        elif final is not None:
            decision, reason, decided = str(final.get("status")), _text(final.get("resolution_reason")), final
        else:
            # No terminal edit was ever queued; the call ends as its deadline would have ended it.
            decision, reason, decided = "expired", approval_cards.TIMEOUT_REASON, {}
            self._final_edits.append(call)
        self.calls[call] = {
            "principal_id": principal_id,
            "owner_kind": "reply",
            "call_ordinal": self._next_ordinal(call),
            "tool_name": _text(initial.get("tool_name")),
            "toolkit_name": None,
            "invoking_agent": _text(initial.get("agent_name")),
            "arguments_digest": None,
            "human_approval_required": None,
            "approver_user_id": _text(initial.get("approver_user_id")),
            "expires_at_ns": _ns(initial.get("expires_at")) or 0,
            "decision": decision,
            "reason": reason,
            "resolved_by": _text(decided.get("resolved_by")),
            "resolved_at_ns": _ns(decided.get("resolved_at")),
            "approved_scope": None,
            "grant_id": grant_id,
        }

    def _next_ordinal(self, call: CallKey) -> int:
        """Order a call after every converted call of its owner's generation."""
        ordinals = [
            int(cast("int", row["call_ordinal"]))
            for key, row in self.calls.items()
            if (key.owner_id, key.generation) == (call.owner_id, call.generation)
        ]
        return max(ordinals, default=-1) + 1

    def insert(self, transaction: Transaction) -> None:
        for call, row in self.calls.items():
            _insert(transaction, "approval_calls", {**row, **_key(call)})
        for (principal_id, delivery_id), row in self.cards.items():
            _insert(transaction, "approval_cards", {"principal_id": principal_id, "delivery_id": delivery_id, **row})
        for (principal_id, event_id), row in self.events.items():
            _insert(
                transaction,
                "approval_card_events",
                {"principal_id": principal_id, "card_event_id": event_id, **row},
            )
        for (principal_id, delivery_id), row in self.scopes.items():
            _insert(
                transaction,
                "approval_card_scopes",
                {"principal_id": principal_id, "delivery_id": delivery_id, **row},
            )
        for row in self.grants:
            _insert(transaction, "approval_grants", row)
        for row in self.bindings:
            _insert(transaction, "scheduled_call_approvals", row)
        for call in self._final_edits:
            card = approval_cards.request_card(transaction, call)
            if card is not None:
                approval_cards.queue_terminal_edit(
                    transaction,
                    card,
                    status="expired",
                    reason=approval_cards.TIMEOUT_REASON,
                    metadata=None,
                )


def _key(call: CallKey) -> dict[str, object]:
    return {"owner_id": call.owner_id, "generation": call.generation, "tool_call_id": call.tool_call_id}


def _insert(transaction: Transaction, table: str, row: dict[str, object]) -> None:
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _column in row)
    transaction.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608 - fixed tables and columns
        tuple(row.values()),
    )
