"""Durable pending terminal delivery store: precedence, restart repair, retention."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.message_target import MessageTarget
from mindroom.terminal_delivery import (
    TERMINAL_DELIVERY_SCHEMA_VERSION,
    PendingTerminalDelivery,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    TerminalOutcomeKind,
    _reset_terminal_delivery_store_runtime,
    terminal_delivery_id,
)
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from pathlib import Path


ROOM_ID = "!room:localhost"
SOURCE_EVENT_ID = "$source"
TARGET_EVENT_ID = "$placeholder"


class _Clock:
    """Deterministic wall clock for store scheduling assertions."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_store_runtime() -> None:
    """Keep process-wide store state from leaking between tests."""
    _reset_terminal_delivery_store_runtime()
    yield
    _reset_terminal_delivery_store_runtime()


def _target(room_id: str = ROOM_ID) -> MessageTarget:
    return MessageTarget.resolve(room_id, None, SOURCE_EVENT_ID)


def _intent(
    *,
    outcome_kind: TerminalOutcomeKind = "completed",
    body: str = "final answer",
    correlation_id: str = "corr-1",
    target_event_id: str | None = TARGET_EVENT_ID,
    source_event_ids: tuple[str, ...] = (SOURCE_EVENT_ID,),
    anchor_event_id: str = SOURCE_EVENT_ID,
    room_id: str = ROOM_ID,
) -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        agent_name="helper",
        target=_target(room_id),
        target_event_id=target_event_id,
        anchor_event_id=anchor_event_id,
        source_event_ids=source_event_ids,
        outcome_kind=outcome_kind,
        body=body,
        correlation_id=correlation_id,
        response_kind="ai",
        tool_trace=(ToolTraceEntry(type="tool_call_completed", tool_name="shell", args_preview="ls"),),
        extra_content={"io.mindroom.stream_status": "completed"},
    )


def _store(tmp_path: Path, clock: _Clock | None = None) -> TerminalDeliveryStore:
    return TerminalDeliveryStore(
        agent_name="helper",
        base_path=tmp_path / "tracking",
        clock=clock or _Clock(),
        attempt_lease_seconds=60.0,
    )


class TestRecording:
    """Recording one committed terminal intent."""

    def test_records_a_schedulable_pending_row(self, tmp_path: Path) -> None:
        """A recorded intent becomes a due pending row on disk."""
        store = _store(tmp_path)

        recorded = store.record(_intent())

        assert recorded is not None
        assert recorded.state == "pending"
        assert recorded.revision == 0
        assert recorded.body == "final answer"
        assert recorded.delivery_kind == "edit"
        assert recorded.delivery_id == terminal_delivery_id(
            agent_name="helper",
            room_id=ROOM_ID,
            target_event_id=TARGET_EVENT_ID,
            anchor_event_id=SOURCE_EVENT_ID,
        )
        assert store.pending_target_event_ids(ROOM_ID) == frozenset({TARGET_EVENT_ID})

    def test_recording_the_same_intent_twice_is_idempotent(self, tmp_path: Path) -> None:
        """A duplicate record keeps the existing row instead of resetting its schedule."""
        store = _store(tmp_path)
        first = store.record(_intent())
        assert first is not None
        store.defer(first.delivery_id, reason="rate_limited:M_LIMIT_EXCEEDED", next_attempt_at=9_999.0)

        duplicate = store.record(_intent())

        assert duplicate is not None
        assert duplicate.state == "superseded"
        stored = store.get(first.delivery_id)
        assert stored is not None
        assert stored.state == "retry_wait"
        assert stored.attempts == 1
        assert stored.next_attempt_at == 9_999.0

    def test_unserializable_extra_content_is_rejected(self, tmp_path: Path) -> None:
        """Content that cannot be persisted is refused rather than silently dropped."""
        store = _store(tmp_path)

        recorded = store.record(
            TerminalDeliveryIntent(
                agent_name="helper",
                target=_target(),
                target_event_id=TARGET_EVENT_ID,
                anchor_event_id=SOURCE_EVENT_ID,
                source_event_ids=(SOURCE_EVENT_ID,),
                outcome_kind="completed",
                body="final",
                extra_content={"bad": object()},
            ),
        )

        assert recorded is None
        assert store.unsettled_items() == ()


class TestPrecedence:
    """Ordering rules that stop stale outcomes overwriting newer ones."""

    def test_success_replaces_a_pending_error_for_the_same_turn(self, tmp_path: Path) -> None:
        """A successful final response wins over an error recorded for the same turn."""
        store = _store(tmp_path)
        store.record(_intent(outcome_kind="error", body="failed"))

        recorded = store.record(_intent(outcome_kind="completed", body="final answer"))

        assert recorded is not None
        assert recorded.state == "pending"
        assert recorded.outcome_kind == "completed"
        assert recorded.revision == 1
        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.body == "final answer"

    def test_delayed_error_cannot_overwrite_a_pending_success(self, tmp_path: Path) -> None:
        """A late fallback error does not displace the pending successful response."""
        store = _store(tmp_path)
        success = store.record(_intent(outcome_kind="completed", body="final answer"))
        assert success is not None

        stale = store.record(_intent(outcome_kind="error", body="Response delivery failed."))

        assert stale is not None
        assert stale.state == "superseded"
        stored = store.get(success.delivery_id)
        assert stored is not None
        assert stored.outcome_kind == "completed"
        assert stored.body == "final answer"

    def test_delayed_error_cannot_overwrite_a_delivered_success(self, tmp_path: Path) -> None:
        """Once a successful final response is visible, a delayed error stays out."""
        store = _store(tmp_path)
        success = store.record(_intent(outcome_kind="completed"))
        assert success is not None
        store.mark_delivered(success.delivery_id)

        stale = store.record(_intent(outcome_kind="error", body="Response delivery failed."))

        assert stale is not None
        assert stale.state == "superseded"
        stored = store.get(success.delivery_id)
        assert stored is not None
        assert stored.state == "delivered"
        assert stored.outcome_kind == "completed"

    def test_newer_response_turn_supersedes_an_older_terminal_intent(self, tmp_path: Path) -> None:
        """A regenerated response for the same target replaces the stale intent."""
        store = _store(tmp_path)
        older = store.record(_intent(correlation_id="corr-1", body="old answer"))
        assert older is not None

        regenerated = store.record(_intent(correlation_id="corr-2", body="new answer"))

        assert regenerated is not None
        assert regenerated.state == "pending"
        assert regenerated.revision == 1
        stored = store.get(older.delivery_id)
        assert stored is not None
        assert stored.body == "new answer"
        assert stored.correlation_id == "corr-2"

    def test_transaction_id_is_stable_per_revision(self, tmp_path: Path) -> None:
        """Retrying one revision reuses one Matrix transaction ID; a new revision does not."""
        store = _store(tmp_path)
        first = store.record(_intent(correlation_id="corr-1"))
        assert first is not None
        second = store.record(_intent(correlation_id="corr-2"))
        assert second is not None

        assert first.transaction_id == f"mindroom-td-{first.delivery_id}-0"
        assert second.transaction_id == f"mindroom-td-{first.delivery_id}-1"


class TestRedactionAndCancellation:
    """Settling rows whose source or target no longer exists."""

    def test_source_redaction_supersedes_pending_delivery(self, tmp_path: Path) -> None:
        """A redacted source event cancels the durable retry it owns."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None

        cancelled = store.supersede_sources((SOURCE_EVENT_ID,), reason="source_event_redacted")

        assert cancelled == (recorded.delivery_id,)
        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.state == "superseded"
        assert stored.settled_reason == "source_event_redacted"
        assert store.pending_target_event_ids() == frozenset()

    def test_target_redaction_supersedes_pending_delivery(self, tmp_path: Path) -> None:
        """A redacted visible target is never recreated by durable retry."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None

        store.supersede_target_event(room_id=ROOM_ID, target_event_id=TARGET_EVENT_ID, reason="target_event_redacted")

        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.state == "superseded"

    def test_settled_rows_are_not_resurrected_by_supersede(self, tmp_path: Path) -> None:
        """Superseding never reopens an already delivered row."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None
        store.mark_delivered(recorded.delivery_id)

        assert store.supersede_sources((SOURCE_EVENT_ID,), reason="source_event_redacted") == ()
        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.state == "delivered"


class TestLeasingAndRestart:
    """Claiming work and repairing it after a crash."""

    def test_claim_due_leases_only_due_rows(self, tmp_path: Path) -> None:
        """Rows scheduled in the future stay out of the claimed batch."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        due = store.record(_intent())
        assert due is not None
        later = store.record(_intent(target_event_id="$other", anchor_event_id="$other-source"))
        assert later is not None
        store.defer(later.delivery_id, reason="rate_limited:M_LIMIT_EXCEEDED", next_attempt_at=clock.now + 30)

        claimed = store.claim_due(limit=10)

        assert [item.delivery_id for item in claimed] == [due.delivery_id]
        assert claimed[0].state == "attempting"
        assert claimed[0].lease_expires_at == clock.now + 60.0

    def test_warm_returns_leaked_attempts_to_the_retry_queue(self, tmp_path: Path) -> None:
        """A crash mid-attempt leaves exactly one valid recoverable state."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        store.claim_due(limit=1)

        _reset_terminal_delivery_store_runtime()
        restarted = _store(tmp_path, clock)
        recovered = restarted.warm()

        assert [item.delivery_id for item in recovered] == [recorded.delivery_id]
        reloaded = restarted.get(recorded.delivery_id)
        assert reloaded is not None
        assert reloaded.state == "retry_wait"
        assert reloaded.next_attempt_at <= clock.now
        assert reloaded.body == "final answer"
        assert reloaded.tool_trace[0].tool_name == "shell"
        assert reloaded.target.room_id == ROOM_ID

    def test_expired_lease_is_reclaimed_without_restart(self, tmp_path: Path) -> None:
        """A lease that outlives its window is retried rather than lost."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        store.claim_due(limit=1)
        clock.advance(61.0)

        reclaimed = store.claim_due(limit=1)

        assert [item.delivery_id for item in reclaimed] == [recorded.delivery_id]

    def test_release_returns_a_lease_without_counting_an_attempt(self, tmp_path: Path) -> None:
        """Shutdown during an attempt keeps the row retryable and its budget intact."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None
        store.claim_due(limit=1)

        store.release(recorded.delivery_id, reason="worker_shutdown")

        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.state == "retry_wait"
        assert stored.attempts == 0


class TestStorageResilience:
    """Corruption handling and retention."""

    def test_malformed_store_file_is_quarantined(self, tmp_path: Path) -> None:
        """A corrupt store file is moved aside and the store keeps working."""
        store = _store(tmp_path)
        store.record(_intent())
        _reset_terminal_delivery_store_runtime()
        store.store_file.write_text("{not json", encoding="utf-8")

        reopened = _store(tmp_path)
        reopened.warm()

        assert reopened.items() == ()
        assert list(store.store_file.parent.glob("*.corrupt-*"))
        assert reopened.record(_intent()) is not None

    def test_invalid_rows_are_dropped_while_valid_rows_survive(self, tmp_path: Path) -> None:
        """One malformed row is quarantined without losing its healthy siblings."""
        store = _store(tmp_path)
        healthy = store.record(_intent())
        assert healthy is not None
        _reset_terminal_delivery_store_runtime()
        payload = json.loads(store.store_file.read_text(encoding="utf-8"))
        payload["items"]["corrupt-row"] = {"delivery_id": "corrupt-row", "state": "not-a-state"}
        store.store_file.write_text(json.dumps(payload), encoding="utf-8")

        reopened = _store(tmp_path)
        reopened.warm()

        assert [item.delivery_id for item in reopened.items()] == [healthy.delivery_id]

    def test_unsupported_schema_is_quarantined(self, tmp_path: Path) -> None:
        """A future or legacy schema is refused rather than misparsed."""
        store = _store(tmp_path)
        store.record(_intent())
        _reset_terminal_delivery_store_runtime()
        store.store_file.write_text(
            json.dumps({"schema_version": TERMINAL_DELIVERY_SCHEMA_VERSION + 1, "items": {}}),
            encoding="utf-8",
        )

        reopened = _store(tmp_path)
        reopened.warm()

        assert reopened.items() == ()

    def test_compaction_keeps_pending_rows_and_drops_old_settled_rows(self, tmp_path: Path) -> None:
        """Age compacts settled rows only; pending work never disappears because of age."""
        clock = _Clock()
        store = TerminalDeliveryStore(
            agent_name="helper",
            base_path=tmp_path / "tracking",
            clock=clock,
            settled_retention_seconds=100.0,
        )
        settled = store.record(_intent(target_event_id="$settled", anchor_event_id="$settled-source"))
        assert settled is not None
        store.mark_delivered(settled.delivery_id)
        pending = store.record(_intent())
        assert pending is not None

        clock.advance(1_000.0)
        store.warm()

        assert [item.delivery_id for item in store.items()] == [pending.delivery_id]

    def test_backlog_snapshot_reports_bounded_observability(self, tmp_path: Path) -> None:
        """Backlog reporting stays counts-only and never carries response bodies."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        store.record(_intent())
        clock.advance(5.0)

        backlog = store.backlog()

        assert backlog.unsettled_count == 1
        assert backlog.dead_letter_count == 0
        assert backlog.oldest_unsettled_age_seconds == pytest.approx(5.0)
        assert backlog.unsettled_by_room == {ROOM_ID: 1}
        assert backlog.unsettled_by_outcome == {"completed": 1}

    def test_persisted_record_contains_no_transport_secrets(self, tmp_path: Path) -> None:
        """The durable row never stores clients, tokens, or auth material."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None

        payload = json.loads(store.store_file.read_text(encoding="utf-8"))
        row = payload["items"][recorded.delivery_id]

        assert set(row) == set(PendingTerminalDelivery.to_record(recorded))
        assert not {key for key in row if "token" in key or "secret" in key or "client" in key}
