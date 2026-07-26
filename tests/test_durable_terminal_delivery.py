"""Durable terminal delivery: store precedence, retry worker, and the gateway seam.

Once a terminal outcome is committed, transport failure persists a durable
intent, a later attempt makes it visible, and startup stale-stream cleanup
leaves that pending repair alone.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.exceptions import SendRetryError

from mindroom.config.main import AgentConfig, Config
from mindroom.constants import STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.delivery_gateway import (
    _DURABLE_TERMINAL_RETRY_FAILURE_REASON,
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    ResponseIdentity,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.stale_stream_cleanup import StaleStreamCleanupActor, recover_stale_streaming_messages
from mindroom.message_target import MessageTarget
from mindroom.redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps
from mindroom.terminal_delivery import (
    TERMINAL_DELIVERY_SCHEMA_VERSION,
    PendingTerminalDelivery,
    TerminalDeliveryAttempt,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    _reset_terminal_delivery_store_runtime,
)
from mindroom.terminal_delivery_worker import TerminalDeliveryWorker, TerminalDeliveryWorkerDeps
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, message_origin, test_runtime_paths
from tests.event_cache_test_support import raw_nio_redaction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import structlog

    from mindroom.constants import RuntimePaths

ROOM_ID = "!test:localhost"
SOURCE_EVENT_ID = "$source"
PLACEHOLDER_EVENT_ID = "$placeholder"
FINAL_BODY = "the committed final answer"
_STATE_WAIT_TIMEOUT_SECONDS = 5.0


class _Clock:
    """Deterministic wall clock shared by store and worker."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_store_runtime() -> None:
    """Keep process-wide durable store state from leaking between tests."""
    _reset_terminal_delivery_store_runtime()
    yield
    _reset_terminal_delivery_store_runtime()


@pytest.fixture(autouse=True)
def _fast_sync_recovery_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exhaust the immediate recovery-retry budget quickly; durability is under test."""
    monkeypatch.setattr("mindroom.matrix.client_delivery._SYNC_RECOVERY_RETRY_TIMEOUT_SECONDS", 0.05)


def _store(tmp_path: Path, clock: _Clock | None = None) -> TerminalDeliveryStore:
    return TerminalDeliveryStore(
        agent_name="helper",
        base_path=tmp_path / "tracking",
        clock=clock or _Clock(),
        attempt_lease_seconds=60.0,
    )


def _intent(
    *,
    body: str = FINAL_BODY,
    correlation_id: str = "corr-1",
    target_event_id: str = PLACEHOLDER_EVENT_ID,
    source_event_id: str = SOURCE_EVENT_ID,
    room_id: str = ROOM_ID,
) -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        agent_name="helper",
        target=MessageTarget.resolve(room_id, None, source_event_id),
        target_event_id=target_event_id,
        anchor_event_id=source_event_id,
        source_event_ids=(source_event_id,),
        body=body,
        correlation_id=correlation_id,
        tool_trace=(ToolTraceEntry(type="tool_call_completed", tool_name="shell", args_preview="ls"),),
        extra_content={STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
    )


class TestStore:
    """Durable record, precedence, restart repair, and retention."""

    def test_records_a_schedulable_pending_row(self, tmp_path: Path) -> None:
        """A recorded intent becomes a due pending row on disk."""
        store = _store(tmp_path)

        recorded = store.record(_intent())

        assert recorded is not None
        assert recorded.state == "pending"
        assert recorded.revision == 0
        assert recorded.body == FINAL_BODY
        assert store.pending_target_event_ids(ROOM_ID) == frozenset({PLACEHOLDER_EVENT_ID})

    def test_recording_the_same_turn_twice_is_idempotent(self, tmp_path: Path) -> None:
        """A duplicate record keeps the existing row and its retry budget."""
        store = _store(tmp_path)
        first = store.record(_intent())
        assert first is not None
        store.defer(first.delivery_id, reason="edit_failed", next_attempt_at=9_999.0)

        assert store.record(_intent()) is None
        stored = store.get(first.delivery_id)
        assert stored is not None
        assert stored.attempts == 1
        assert stored.next_attempt_at == 9_999.0

    def test_newer_response_turn_supersedes_an_older_terminal_intent(self, tmp_path: Path) -> None:
        """A regenerated response for the same target replaces the stale intent."""
        store = _store(tmp_path)
        older = store.record(_intent(correlation_id="corr-1", body="old answer"))
        assert older is not None

        regenerated = store.record(_intent(correlation_id="corr-2", body="new answer"))

        assert regenerated is not None
        assert regenerated.revision == 1
        stored = store.get(older.delivery_id)
        assert stored is not None
        assert stored.body == "new answer"

    def test_transaction_id_is_stable_per_revision(self, tmp_path: Path) -> None:
        """Retrying one revision reuses one transaction ID; a new revision does not."""
        store = _store(tmp_path)
        first = store.record(_intent(correlation_id="corr-1"))
        second = store.record(_intent(correlation_id="corr-2"))
        assert first is not None
        assert second is not None

        assert first.transaction_id == f"mindroom-td-{first.delivery_id}-0"
        assert second.transaction_id == f"mindroom-td-{first.delivery_id}-1"

    def test_unserializable_extra_content_is_rejected(self, tmp_path: Path) -> None:
        """Content that cannot be persisted is refused rather than silently dropped."""
        store = _store(tmp_path)

        recorded = store.record(
            TerminalDeliveryIntent(
                agent_name="helper",
                target=MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID),
                target_event_id=PLACEHOLDER_EVENT_ID,
                anchor_event_id=SOURCE_EVENT_ID,
                source_event_ids=(SOURCE_EVENT_ID,),
                body="final",
                extra_content={"bad": object()},
            ),
        )

        assert recorded is None
        assert store.unsettled_items() == ()

    def test_source_and_target_redaction_supersede_pending_delivery(self, tmp_path: Path) -> None:
        """A redacted question or answer cancels the durable retry it owns."""
        store = _store(tmp_path)
        by_source = store.record(_intent())
        assert by_source is not None
        assert store.supersede_sources((SOURCE_EVENT_ID,), reason="source_event_redacted") == (by_source.delivery_id,)
        settled = store.get(by_source.delivery_id)
        assert settled is not None
        assert settled.state == "superseded"
        assert store.pending_target_event_ids() == frozenset()

        by_target = store.record(_intent(correlation_id="corr-2"))
        assert by_target is not None
        store.supersede_target_event(
            room_id=ROOM_ID,
            target_event_id=PLACEHOLDER_EVENT_ID,
            reason="target_event_redacted",
        )
        assert store.unsettled_items() == ()

    def test_settled_rows_are_not_resurrected(self, tmp_path: Path) -> None:
        """Superseding never reopens an already delivered row."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None
        store.mark_delivered(recorded.delivery_id)

        assert store.supersede_sources((SOURCE_EVENT_ID,), reason="source_event_redacted") == ()
        stored = store.get(recorded.delivery_id)
        assert stored is not None
        assert stored.state == "delivered"

    def test_claim_due_leases_only_due_rows(self, tmp_path: Path) -> None:
        """Rows scheduled in the future stay out of the claimed batch."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        due = store.record(_intent())
        later = store.record(_intent(target_event_id="$other", source_event_id="$other-source"))
        assert due is not None
        assert later is not None
        store.defer(later.delivery_id, reason="edit_failed", next_attempt_at=clock.now + 30)

        claimed = store.claim_due(limit=10)

        assert [item.delivery_id for item in claimed] == [due.delivery_id]
        assert claimed[0].state == "attempting"
        assert claimed[0].lease_expires_at == clock.now + 60.0

    def test_restart_returns_leaked_attempts_to_the_retry_queue(self, tmp_path: Path) -> None:
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
        assert reloaded.body == FINAL_BODY
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

        assert [item.delivery_id for item in store.claim_due(limit=1)] == [recorded.delivery_id]

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

    @pytest.mark.parametrize(
        "corrupt",
        [
            pytest.param(lambda _payload: "{not json", id="malformed"),
            pytest.param(
                lambda _payload: json.dumps({"schema_version": TERMINAL_DELIVERY_SCHEMA_VERSION + 1, "items": {}}),
                id="unsupported-schema",
            ),
        ],
    )
    def test_unreadable_store_is_quarantined(
        self,
        tmp_path: Path,
        corrupt: Callable[[dict[str, object]], str],
    ) -> None:
        """A corrupt or wrong-schema file is moved aside and the store keeps working."""
        store = _store(tmp_path)
        store.record(_intent())
        _reset_terminal_delivery_store_runtime()
        payload = json.loads(store.store_file.read_text(encoding="utf-8"))
        store.store_file.write_text(corrupt(payload), encoding="utf-8")

        reopened = _store(tmp_path)
        reopened.warm()

        assert reopened.items() == ()
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

    def test_retention_keeps_pending_rows_and_drops_old_settled_rows(self, tmp_path: Path) -> None:
        """Age compacts settled rows only; pending work never disappears because of age."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        settled = store.record(_intent(target_event_id="$settled", source_event_id="$settled-source"))
        assert settled is not None
        store.mark_delivered(settled.delivery_id)
        pending = store.record(_intent())
        assert pending is not None

        clock.advance(48 * 60 * 60)
        store.warm()

        assert [item.delivery_id for item in store.items()] == [pending.delivery_id]

    def test_persisted_record_contains_no_transport_secrets(self, tmp_path: Path) -> None:
        """The durable row never stores clients, tokens, or auth material."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None

        row = json.loads(store.store_file.read_text(encoding="utf-8"))["items"][recorded.delivery_id]

        assert set(row) == set(recorded.to_record())
        assert not {key for key in row if "token" in key or "secret" in key or "client" in key}


def _worker(
    store: TerminalDeliveryStore,
    attempt: Callable[[PendingTerminalDelivery], Awaitable[TerminalDeliveryAttempt]],
    *,
    clock: _Clock,
    max_concurrency: int = 4,
    max_attempts: int = 12,
    is_ready: Callable[[], bool] | None = None,
    poll_interval_seconds: float = 600.0,
) -> TerminalDeliveryWorker:
    return TerminalDeliveryWorker(
        TerminalDeliveryWorkerDeps(
            store=store,
            attempt=attempt,
            is_ready=is_ready or (lambda: True),
            logger=MagicMock(),
            wall_clock=clock,
            jitter=lambda: 1.0,
            poll_interval_seconds=poll_interval_seconds,
            max_concurrency=max_concurrency,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=8.0,
            max_attempts=max_attempts,
        ),
    )


async def _wait_for_state(store: TerminalDeliveryStore, delivery_id: str, state: str) -> None:
    """Wait until one durable record reaches an expected state."""
    async with asyncio.timeout(_STATE_WAIT_TIMEOUT_SECONDS):
        while True:
            item = store.get(delivery_id)
            if item is not None and item.state == state:
                return
            await asyncio.sleep(0.01)


class TestWorker:
    """Retry policy, scheduling limits, and lifecycle."""

    @pytest.mark.asyncio
    async def test_delivery_lands_once_recovery_finishes(self, tmp_path: Path) -> None:
        """Recovery blocking longer than the immediate budget still converges."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        recovery_ready = False
        transactions: list[str] = []

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            transactions.append(item.transaction_id)
            if not recovery_ready:
                return TerminalDeliveryAttempt.transient("edit_failed")
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock)
        await worker.drain_once()
        blocked = store.get(recorded.delivery_id)
        assert blocked is not None
        assert blocked.state == "retry_wait"

        recovery_ready = True
        clock.advance(blocked.next_attempt_at - clock.now)
        await worker.drain_once()

        delivered = store.get(recorded.delivery_id)
        assert delivered is not None
        assert delivered.state == "delivered"
        # One revision reuses one Matrix transaction ID, so an at-least-once
        # transport still has an exactly-once visible effect.
        assert len(set(transactions)) == 1

    @pytest.mark.asyncio
    async def test_backoff_grows_and_is_bounded(self, tmp_path: Path) -> None:
        """Transient failures back off exponentially up to the configured ceiling."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("edit_failed")

        worker = _worker(store, attempt, clock=clock)
        delays: list[float] = []
        for _round in range(5):
            await worker.drain_once()
            item = store.get(recorded.delivery_id)
            assert item is not None
            delays.append(item.next_attempt_at - clock.now)
            clock.advance(item.next_attempt_at - clock.now)

        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]

    @pytest.mark.asyncio
    async def test_retry_budget_exhaustion_dead_letters(self, tmp_path: Path) -> None:
        """A row that can never land stops retrying and is loudly dead-lettered."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("edit_failed")

        worker = _worker(store, attempt, clock=clock, max_attempts=3)
        for _round in range(3):
            await worker.drain_once()
            item = store.get(recorded.delivery_id)
            assert item is not None
            clock.advance(max(0.0, item.next_attempt_at - clock.now))

        settled = store.get(recorded.delivery_id)
        assert settled is not None
        assert settled.state == "dead_letter"
        assert settled.settled_reason == "retry_budget_exhausted:edit_failed"

    @pytest.mark.asyncio
    async def test_superseded_attempt_is_not_retried(self, tmp_path: Path) -> None:
        """A vanished visible target ends the retry instead of looping forever."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        attempts = 0

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal attempts
            attempts += 1
            return TerminalDeliveryAttempt.superseded("target_event_missing")

        worker = _worker(store, attempt, clock=clock)
        await worker.drain_once()
        clock.advance(600.0)
        await worker.drain_once()

        settled = store.get(recorded.delivery_id)
        assert settled is not None
        assert settled.state == "superseded"
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_attempt_exception_is_treated_as_transient(self, tmp_path: Path) -> None:
        """An unexpected attempt error retries rather than losing the outcome."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            msg = "boom"
            raise RuntimeError(msg)

        await _worker(store, attempt, clock=clock).drain_once()

        item = store.get(recorded.delivery_id)
        assert item is not None
        assert item.state == "retry_wait"
        assert item.last_error == "attempt_exception"

    @pytest.mark.asyncio
    async def test_backlog_respects_concurrency_and_per_room_order(self, tmp_path: Path) -> None:
        """Fifty rows across ten rooms drain fully, bounded, and in per-room order."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        expected_order: dict[str, list[str]] = {}
        for room_index in range(10):
            room_id = f"!room{room_index}:localhost"
            expected_order[room_id] = []
            for item_index in range(5):
                source_event_id = f"$r{room_index}-i{item_index}"
                recorded = store.record(
                    _intent(room_id=room_id, source_event_id=source_event_id, target_event_id=f"{source_event_id}-p"),
                )
                assert recorded is not None
                expected_order[room_id].append(recorded.delivery_id)
                # Distinct scheduling times make the claim order deterministic.
                clock.advance(0.001)

        in_flight = 0
        peak_in_flight = 0
        per_room_in_flight: dict[str, int] = {}
        peak_per_room = 0
        completion_order: dict[str, list[str]] = {room_id: [] for room_id in expected_order}

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal in_flight, peak_in_flight, peak_per_room
            room_id = item.target.room_id
            in_flight += 1
            per_room_in_flight[room_id] = per_room_in_flight.get(room_id, 0) + 1
            peak_in_flight = max(peak_in_flight, in_flight)
            peak_per_room = max(peak_per_room, per_room_in_flight[room_id])
            await asyncio.sleep(0)
            completion_order[room_id].append(item.delivery_id)
            per_room_in_flight[room_id] -= 1
            in_flight -= 1
            return TerminalDeliveryAttempt.delivered_now()

        attempted = await _worker(store, attempt, clock=clock, max_concurrency=4).drain_once()

        assert attempted == 50
        assert peak_in_flight <= 4
        assert peak_per_room == 1
        assert completion_order == expected_order
        assert store.unsettled_items() == ()

    @pytest.mark.asyncio
    async def test_wake_drains_without_waiting_for_the_poll_interval(self, tmp_path: Path) -> None:
        """A recovery-ready wakeup delivers immediately instead of after the scan."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock)
        worker.start()
        try:
            worker.wake(reason="sync_response_applied")
            await _wait_for_state(store, recorded.delivery_id, "delivered")
        finally:
            await worker.stop()

        assert not worker.running

    @pytest.mark.asyncio
    async def test_shutdown_during_an_attempt_keeps_the_row_retryable(self, tmp_path: Path) -> None:
        """Stopping mid-attempt returns the lease instead of losing the outcome."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        started = asyncio.Event()

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            started.set()
            await asyncio.sleep(3600)
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock)
        worker.start()
        await asyncio.wait_for(started.wait(), timeout=_STATE_WAIT_TIMEOUT_SECONDS)
        await worker.stop()

        item = store.get(recorded.delivery_id)
        assert item is not None
        assert item.state == "retry_wait"
        assert item.last_error == "worker_shutdown"
        assert item.attempts == 0
        assert [task for task in asyncio.all_tasks() if task.get_name() == "terminal_delivery_worker"] == []

    @pytest.mark.asyncio
    async def test_worker_stays_idle_until_the_runtime_is_ready(self, tmp_path: Path) -> None:
        """Delivery never runs before the bot has a live, synced Matrix client."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None
        attempted = 0
        readiness = {"ready": False}

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            nonlocal attempted
            attempted += 1
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, is_ready=lambda: readiness["ready"])
        worker.start()
        try:
            worker.wake(reason="not_ready_yet")
            await asyncio.sleep(0.05)
            assert attempted == 0
            readiness["ready"] = True
            worker.wake(reason="test_ready")
            await _wait_for_state(store, recorded.delivery_id, "delivered")
        finally:
            await worker.stop()

        assert attempted == 1


def _config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(agents={"helper": AgentConfig(display_name="HelperAgent", rooms=[ROOM_ID])}),
        runtime_paths,
    )
    return config, runtime_paths


def _envelope(target: MessageTarget) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=SOURCE_EVENT_ID,
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="helper",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )


def _identity(target: MessageTarget, *, correlation_id: str = "corr-durable") -> ResponseIdentity:
    return ResponseIdentity(
        response_kind="ai",
        response_envelope=_envelope(target),
        correlation_id=correlation_id,
    )


def _gateway(
    *,
    tmp_path: Path,
    client: nio.AsyncClient,
    target: MessageTarget,
    with_store: bool = True,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> tuple[DeliveryGateway, TerminalDeliveryStore | None]:
    config, runtime_paths = _config(tmp_path)
    store = TerminalDeliveryStore(agent_name="helper", base_path=tmp_path / "tracking") if with_store else None
    envelope = _envelope(target)
    response_hooks = SimpleNamespace(
        apply_before_response=AsyncMock(
            return_value=SimpleNamespace(
                response_text=FINAL_BODY,
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=envelope,
                suppress=False,
            ),
        ),
        apply_final_response_transform=AsyncMock(
            return_value=SimpleNamespace(response_text=FINAL_BODY, response_kind="ai", envelope=envelope),
        ),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    resolver = MagicMock()
    resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)
    resolver.deps.conversation_cache.notify_outbound_message = MagicMock()
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=client, orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths,
            agent_name="helper",
            logger=logger or MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=resolver,
            response_hooks=response_hooks,
            terminal_delivery_store=store,
        ),
    )
    return gateway, store


def _stream_outcome(
    *,
    failure_reason: str = "terminal_update_failed",
    terminal_status: Literal["completed", "cancelled", "error"] = "completed",
) -> StreamTransportOutcome:
    return StreamTransportOutcome(
        last_physical_stream_event_id=PLACEHOLDER_EVENT_ID,
        terminal_status=terminal_status,
        rendered_body="partial strea",
        visible_body_state="visible_body",
        canonical_final_body_candidate=FINAL_BODY,
        failure_reason=failure_reason,
    )


async def _finalize_failed_stream(
    gateway: DeliveryGateway,
    target: MessageTarget,
    *,
    stream_outcome: StreamTransportOutcome | None = None,
    extra_content: dict[str, Any] | None = None,
) -> FinalDeliveryOutcome:
    return await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=target,
            stream_transport_outcome=stream_outcome or _stream_outcome(),
            initial_delivery_kind="sent",
            identity=_identity(target),
            tool_trace=None,
            extra_content=extra_content,
        ),
    )


class TestGatewaySeam:
    """Recording committed outcomes and repairing them against a live client."""

    @pytest.mark.asyncio
    async def test_stream_terminal_edit_failure_persists_the_final_body(self, tmp_path: Path) -> None:
        """Recovery blocking past the immediate budget leaves a durable pending final."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        assert store is not None

        outcome = await _finalize_failed_stream(gateway, target, extra_content={STREAM_STATUS_KEY: "streaming"})

        assert outcome.failure_reason == _DURABLE_TERMINAL_RETRY_FAILURE_REASON
        pending = store.unsettled_items()
        assert len(pending) == 1
        assert pending[0].target_event_id == PLACEHOLDER_EVENT_ID
        assert FINAL_BODY in pending[0].body
        # A carried in-progress status never survives into the repaired edit.
        assert (pending[0].extra_content or {})[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_reason", "terminal_status"),
        [("model_error", "completed"), ("terminal_update_failed", "error")],
    )
    async def test_only_transport_failures_of_completed_streams_persist(
        self,
        tmp_path: Path,
        failure_reason: str,
        terminal_status: Literal["completed", "cancelled", "error"],
    ) -> None:
        """A model error, or a non-completed terminal outcome, is not durably retried."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        assert store is not None

        await _finalize_failed_stream(
            gateway,
            target,
            stream_outcome=_stream_outcome(failure_reason=failure_reason, terminal_status=terminal_status),
        )

        assert store.unsettled_items() == ()

    @pytest.mark.asyncio
    async def test_final_edit_failure_persists_instead_of_overwriting_the_placeholder(self, tmp_path: Path) -> None:
        """A failed placeholder finalize keeps the answer instead of a failure note."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert store is not None

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=PLACEHOLDER_EVENT_ID,
                response_text=FINAL_BODY,
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
                existing_event_is_placeholder=True,
            ),
        )

        assert outcome.failure_reason == _DURABLE_TERMINAL_RETRY_FAILURE_REASON
        assert outcome.final_visible_body is None
        assert [item.body for item in store.unsettled_items()] == [FINAL_BODY]

    @pytest.mark.asyncio
    async def test_no_durable_store_keeps_the_previous_failure_behaviour(self, tmp_path: Path) -> None:
        """Without a store the gateway still finalizes a failed placeholder visibly."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        gateway, _store_none = _gateway(tmp_path=tmp_path, client=client, target=target, with_store=False)

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=PLACEHOLDER_EVENT_ID,
                response_text=FINAL_BODY,
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
                existing_event_is_placeholder=True,
            ),
        )

        assert outcome.failure_reason == "delivery_failed"

    @pytest.mark.asyncio
    async def test_pending_final_lands_after_recovery_and_survives_restart(self, tmp_path: Path) -> None:
        """A pending final reloads after restart and the active bot makes it visible."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        blocked_client = make_matrix_client_mock()
        blocked_client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        first_gateway, first_store = _gateway(tmp_path=tmp_path, client=blocked_client, target=target)
        await _finalize_failed_stream(first_gateway, target)
        assert first_store is not None
        assert len(first_store.unsettled_items()) == 1

        _reset_terminal_delivery_store_runtime()
        recovered_client = make_matrix_client_mock()
        recovered_client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID),
        )
        restarted_gateway, restarted_store = _gateway(tmp_path=tmp_path, client=recovered_client, target=target)
        assert restarted_store is not None
        restarted_store.warm()
        item = restarted_store.unsettled_items()[0]

        attempt = await restarted_gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "delivered"
        sent_content = recovered_client.room_send.await_args.kwargs["content"]
        assert sent_content["m.relates_to"] == {"event_id": PLACEHOLDER_EVENT_ID, "rel_type": "m.replace"}
        assert FINAL_BODY in sent_content["m.new_content"]["body"]
        assert recovered_client.room_send.await_args.kwargs["tx_id"] == item.transaction_id

    @pytest.mark.asyncio
    async def test_repeating_the_same_attempt_reuses_one_transaction(self, tmp_path: Path) -> None:
        """A retry after an unacknowledged success repeats one idempotent edit."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await _finalize_failed_stream(gateway, target)
        assert store is not None
        item = store.unsettled_items()[0]

        first = await gateway.attempt_pending_terminal_delivery(item)
        second = await gateway.attempt_pending_terminal_delivery(item)

        assert (first.result, second.result) == ("delivered", "delivered")
        assert {call.kwargs["tx_id"] for call in client.room_send.await_args_list} == {item.transaction_id}
        assert len({call.kwargs["content"]["m.new_content"]["body"] for call in client.room_send.await_args_list}) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("room_get_event", "expected_reason"),
        [
            pytest.param(
                nio.RoomGetEventError.from_dict({"errcode": "M_NOT_FOUND", "error": "not found"}),
                "target_event_missing",
                id="missing",
            ),
            pytest.param(
                nio.RoomGetEventResponse.from_dict(
                    {
                        "event_id": PLACEHOLDER_EVENT_ID,
                        "sender": "@helper:localhost",
                        "origin_server_ts": 1,
                        "type": "m.room.message",
                        "room_id": ROOM_ID,
                        "content": {},
                        "unsigned": {"redacted_because": {"type": "m.room.redaction"}},
                    },
                ),
                "target_event_redacted",
                id="redacted",
            ),
        ],
    )
    async def test_vanished_target_is_superseded_not_recreated(
        self,
        tmp_path: Path,
        room_get_event: object,
        expected_reason: str,
    ) -> None:
        """A missing or redacted response event ends the retry instead of resurrecting it."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_get_event = AsyncMock(return_value=room_get_event)
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await _finalize_failed_stream(gateway, target)
        assert store is not None

        attempt = await gateway.attempt_pending_terminal_delivery(store.unsettled_items()[0])

        assert attempt.result == "superseded"
        assert attempt.reason == expected_reason
        client.room_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replaced_client_is_resolved_per_attempt(self, tmp_path: Path) -> None:
        """A config reload swaps the Matrix client without stranding pending work."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        blocked_client = make_matrix_client_mock()
        blocked_client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        gateway, store = _gateway(tmp_path=tmp_path, client=blocked_client, target=target)
        await _finalize_failed_stream(gateway, target)
        assert store is not None
        item = store.unsettled_items()[0]
        assert (await gateway.attempt_pending_terminal_delivery(item)).result == "transient"

        replacement_client = make_matrix_client_mock()
        replacement_client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID),
        )
        gateway.deps.runtime.client = replacement_client

        assert (await gateway.attempt_pending_terminal_delivery(item)).result == "delivered"
        replacement_client.room_send.assert_awaited()

    @pytest.mark.asyncio
    async def test_attempt_without_a_client_is_transient(self, tmp_path: Path) -> None:
        """A torn-down client defers the attempt instead of failing it permanently."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        await _finalize_failed_stream(gateway, target)
        assert store is not None
        item = store.unsettled_items()[0]
        gateway.deps.runtime.client = None

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert (attempt.result, attempt.reason) == ("transient", "matrix_client_unavailable")

    @pytest.mark.asyncio
    async def test_recording_logs_no_response_body(self, tmp_path: Path) -> None:
        """Durable-retry logging reports shape and counts, never the answer text."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        logger = MagicMock()
        gateway, _store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target, logger=logger)

        await _finalize_failed_stream(gateway, target)

        logged = repr(logger.mock_calls)
        assert "Persisted terminal delivery for durable retry" in logged
        assert FINAL_BODY not in logged
        assert "partial strea" not in logged


class TestRuntimeInteractions:
    """Redaction cancellation and stale-stream cleanup coexistence."""

    @pytest.mark.asyncio
    async def test_source_redaction_cancels_pending_delivery(self, tmp_path: Path) -> None:
        """A redacted question never resurrects its undelivered answer."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        await _finalize_failed_stream(gateway, target)
        assert store is not None
        assert len(store.unsettled_items()) == 1
        conversation_cache = MagicMock()
        conversation_cache.apply_redaction = AsyncMock()
        cleanup = RedactedTurnCleanup(
            RedactedTurnCleanupDeps(
                conversation_cache=conversation_cache,
                turn_store=MagicMock(),
                terminal_delivery_store=store,
            ),
        )
        room = MagicMock()
        room.room_id = ROOM_ID

        await cleanup.handle(
            room,
            raw_nio_redaction(
                {
                    "type": "m.room.redaction",
                    "event_id": "$redaction",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                },
                redacts=SOURCE_EVENT_ID,
            ),
        )

        assert store.unsettled_items() == ()
        assert store.items()[0].settled_reason == "source_event_redacted"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("auto_resume", [False, True])
    async def test_cleanup_skips_a_durably_owned_stream(self, tmp_path: Path, auto_resume: bool) -> None:
        """No interruption note, and no duplicate auto-resumed turn, over a pending final."""
        config, runtime_paths = _config(tmp_path)
        config.defaults.auto_resume_after_restart = auto_resume
        bot_user_id = "@helper:localhost"
        client = make_matrix_client_mock(user_id=bot_user_id)
        client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM_ID]))
        streaming_event = nio.RoomMessageText.from_dict(
            {
                "event_id": PLACEHOLDER_EVENT_ID,
                "sender": bot_user_id,
                "origin_server_ts": 1,
                "type": "m.room.message",
                "room_id": ROOM_ID,
                "content": {"msgtype": "m.text", "body": "partial strea", STREAM_STATUS_KEY: "streaming"},
            },
        )
        client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(room_id=ROOM_ID, chunk=[streaming_event], start="", end=None),
        )
        resume_client = make_matrix_client_mock(user_id=bot_user_id)

        result = await recover_stale_streaming_messages(
            {
                bot_user_id: StaleStreamCleanupActor(
                    client=client,
                    conversation_cache=None,
                    pending_terminal_delivery_event_ids=lambda _room_id: frozenset({PLACEHOLDER_EVENT_ID}),
                ),
            },
            resume_client=resume_client,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            startup_cutoff_ms=None,
            scanned_room_ids=set(),
        )

        assert (result.cleaned_count, result.resumed_count) == (0, 0)
        client.room_send.assert_not_awaited()
        resume_client.room_send.assert_not_awaited()
