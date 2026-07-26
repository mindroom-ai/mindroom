"""Durable terminal delivery: store precedence, retry worker, and the gateway seam.

Once a terminal outcome is committed, transport failure persists a durable
intent, a later attempt makes it visible, and startup stale-stream cleanup
leaves that pending repair alone.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.stale_stream_cleanup import StaleStreamCleanupActor, recover_stale_streaming_messages
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, PostResponseEffectsSupport
from mindroom.redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps
from mindroom.terminal_delivery import (
    TERMINAL_DELIVERY_SCHEMA_VERSION,
    PendingTerminalDelivery,
    TerminalDeliveryAttempt,
    TerminalDeliveryIntent,
    TerminalDeliveryStore,
    _reset_terminal_delivery_store_runtime,
)
from mindroom.terminal_delivery_lifecycle import TerminalDeliveryLifecycleFacts
from mindroom.terminal_delivery_replay import (
    TerminalDeliveryLifecycleReplayer,
    TerminalDeliveryLifecycleReplayerDeps,
)
from mindroom.terminal_delivery_worker import TerminalDeliveryWorker, TerminalDeliveryWorkerDeps
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, message_origin, test_runtime_paths
from tests.event_cache_test_support import raw_nio_redaction

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
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


class _PostEffectRecorder:
    """Real callback recorder for success-only post-response effects."""

    def __init__(self) -> None:
        self.interactive: list[tuple[str, MessageTarget, InteractiveMetadata]] = []
        self.thread_summaries: list[tuple[str, str, str | None]] = []

    async def register_interactive(
        self,
        event_id: str,
        target: MessageTarget,
        metadata: InteractiveMetadata,
    ) -> None:
        self.interactive.append((event_id, target, metadata))

    def build_deps(self, **_kwargs: object) -> PostResponseEffectsDeps:
        return PostResponseEffectsDeps(
            logger=MagicMock(),
            register_interactive=self.register_interactive,
            should_queue_thread_summary=lambda _room_id, _thread_id, _hint: True,
            queue_thread_summary=lambda room_id, thread_id, entity_name: self.thread_summaries.append(
                (room_id, thread_id, entity_name),
            ),
        )


@pytest.fixture(autouse=True)
def _clean_store_runtime() -> Iterator[None]:
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
    target = MessageTarget.resolve(room_id, None, source_event_id)
    envelope = MessageEnvelope(
        source_event_id=source_event_id,
        target=target,
        body="source prompt",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="helper",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )
    return TerminalDeliveryIntent(
        agent_name="helper",
        target=target,
        target_event_id=target_event_id,
        anchor_event_id=source_event_id,
        source_event_ids=(source_event_id,),
        lifecycle=TerminalDeliveryLifecycleFacts(
            response_kind="ai",
            correlation_id=correlation_id,
            response_envelope=envelope,
            interactive_metadata=None,
            thread_summary_message_count_hint=None,
            thread_summary_entity_name="helper",
        ),
        body=body,
        wire_content={
            "msgtype": "m.text",
            "body": f"* {body}",
            "m.new_content": {"msgtype": "m.text", "body": body},
            "m.relates_to": {"rel_type": "m.replace", "event_id": target_event_id},
        },
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
        store.defer(first.delivery_id, revision=first.revision, reason="edit_failed", next_attempt_at=9_999.0)

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
                lifecycle=_intent().lifecycle,
                body="final",
                wire_content={"msgtype": "m.text", "body": "final"},
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
        assert store.get(by_source.delivery_id) is None
        assert store.pending_target_event_ids() == frozenset()

        by_target = store.record(_intent(correlation_id="corr-2"))
        assert by_target is not None
        store.supersede_target_event(
            room_id=ROOM_ID,
            target_event_id=PLACEHOLDER_EVENT_ID,
            reason="target_event_redacted",
        )
        assert store.unsettled_items() == ()

    def test_source_redaction_atomically_removes_a_racing_replacement(self, tmp_path: Path) -> None:
        """A replacement recorded during matching cannot escape source redaction."""
        store = _store(tmp_path)
        recorded = store.record(_intent(correlation_id="corr-1", body="old"))
        assert recorded is not None

        class ReplaceDuringMatch:
            def __iter__(self) -> object:
                replacement = store.record(_intent(correlation_id="corr-2", body="new"))
                assert replacement is not None
                return iter((SOURCE_EVENT_ID,))

        store.supersede_sources(ReplaceDuringMatch(), reason="source_event_redacted")  # type: ignore[arg-type]

        assert store.items() == ()

    def test_delivered_rows_are_not_resurrected(self, tmp_path: Path) -> None:
        """Superseding never reopens a row whose outcome already landed."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None
        store.mark_delivered(recorded.delivery_id, revision=recorded.revision)

        assert store.supersede_sources((SOURCE_EVENT_ID,), reason="source_event_redacted") == ()

    def test_stale_revision_outcome_cannot_settle_a_regenerated_row(self, tmp_path: Path) -> None:
        """An in-flight older attempt must not settle content the newer turn never sent."""
        store = _store(tmp_path)
        first = store.record(_intent(correlation_id="corr-1", body="ORIGINAL answer"))
        assert first is not None
        leased = store.claim_due(limit=1)[0]
        regenerated = store.record(_intent(correlation_id="corr-2", body="REGENERATED answer"))
        assert regenerated is not None

        store.mark_delivered(leased.delivery_id, revision=leased.revision)

        row = store.get(first.delivery_id)
        assert row is not None
        assert row.revision == 1
        assert row.body == "REGENERATED answer"
        # The regenerated body was never transmitted, so it must still be owed.
        assert row.state == "pending"

    @pytest.mark.parametrize(
        "settle",
        [
            pytest.param(
                lambda store, item: store.mark_delivered(item.delivery_id, revision=item.revision),
                id="delivered",
            ),
            pytest.param(
                lambda store, item: store.mark_superseded(item.delivery_id, revision=item.revision, reason="x"),
                id="superseded",
            ),
            pytest.param(
                lambda store, item: store.defer(
                    item.delivery_id,
                    revision=item.revision,
                    reason="x",
                    next_attempt_at=9e9,
                ),
                id="defer",
            ),
            pytest.param(
                lambda store, item: store.release(item.delivery_id, revision=item.revision, reason="x"),
                id="release",
            ),
        ],
    )
    def test_every_transition_is_revision_scoped(
        self,
        tmp_path: Path,
        settle: Callable[[TerminalDeliveryStore, PendingTerminalDelivery], None],
    ) -> None:
        """No stale-revision transition may charge, settle, or reschedule a newer row."""
        store = _store(tmp_path)
        first = store.record(_intent(correlation_id="corr-1"))
        assert first is not None
        leased = store.claim_due(limit=1)[0]
        store.record(_intent(correlation_id="corr-2"))

        settle(store, leased)

        row = store.get(first.delivery_id)
        assert row is not None
        assert (row.state, row.revision, row.attempts) == ("pending", 1, 0)

    def test_release_cannot_resurrect_a_delivered_row(self, tmp_path: Path) -> None:
        """Shutdown releasing a lease must not revive a row whose outcome already landed."""
        store = _store(tmp_path)
        recorded = store.record(_intent())
        assert recorded is not None
        leased = store.claim_due(limit=1)[0]
        store.mark_delivered(leased.delivery_id, revision=leased.revision)

        store.release(leased.delivery_id, revision=leased.revision, reason="worker_shutdown")

        assert store.get(recorded.delivery_id) is None

    def test_claim_due_leases_only_due_rows(self, tmp_path: Path) -> None:
        """Rows scheduled in the future stay out of the claimed batch."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        due = store.record(_intent())
        later = store.record(_intent(target_event_id="$other", source_event_id="$other-source"))
        assert due is not None
        assert later is not None
        store.defer(later.delivery_id, revision=later.revision, reason="edit_failed", next_attempt_at=clock.now + 30)

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

        store.release(recorded.delivery_id, revision=recorded.revision, reason="worker_shutdown")

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

    def test_finished_rows_leave_the_outbox_immediately(self, tmp_path: Path) -> None:
        """The file only ever holds outstanding work, so it cannot grow with uptime."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        finished = store.record(_intent(target_event_id="$settled", source_event_id="$settled-source"))
        assert finished is not None
        store.mark_delivered(finished.delivery_id, revision=finished.revision)
        pending = store.record(_intent())
        assert pending is not None

        assert [item.delivery_id for item in store.items()] == [pending.delivery_id]
        persisted = json.loads(store.store_file.read_text(encoding="utf-8"))["items"]
        assert list(persisted) == [pending.delivery_id]

    def test_more_than_two_thousand_committed_outcomes_remain_retryable(self, tmp_path: Path) -> None:
        """Backlog pressure must never convert committed answers into settled rows."""
        store = _store(tmp_path)
        template = store.record(_intent(target_event_id="$target-0", source_event_id="$source-0"))
        assert template is not None
        with store._state.lock:
            store._state.items = {
                f"delivery-{index}": replace(
                    template,
                    delivery_id=f"delivery-{index}",
                    target_event_id=f"$target-{index}",
                    source_event_ids=(f"$source-{index}",),
                    created_at=float(index),
                )
                for index in range(2_000)
            }

        extra = store.record(_intent(target_event_id="$target-extra", source_event_id="$source-extra"))

        assert extra is not None
        assert len(store.unsettled_items()) == 2_001

    def test_persisted_record_carries_only_repair_and_lifecycle_facts(self, tmp_path: Path) -> None:
        """Transport credentials and live collaborators never enter the durable row."""
        store = _store(tmp_path)
        recorded = store.record(_intent(body="answer referencing nothing secret"))
        assert recorded is not None

        raw = store.store_file.read_text(encoding="utf-8")

        # Values a caller could plausibly leak in: an access token, an auth header,
        # and a live client repr. None are reachable from the persisted fields.
        for secret in ("syt_", "Bearer ", "AsyncClient(", "access_token"):
            assert secret not in raw
        row = json.loads(raw)["items"][recorded.delivery_id]
        assert set(row) == {
            "delivery_id",
            "agent_name",
            "target",
            "target_event_id",
            "anchor_event_id",
            "source_event_ids",
            "lifecycle",
            "revision",
            "body",
            "wire_content",
            "transaction_id",
            "correlation_id",
            "tool_trace",
            "extra_content",
            "runtime_generation",
            "state",
            "attempts",
            "created_at",
            "updated_at",
            "next_attempt_at",
            "last_error",
            "lease_expires_at",
        }
        lifecycle = row["lifecycle"]
        assert set(lifecycle) == {
            "response_kind",
            "correlation_id",
            "response_envelope",
            "interactive_metadata",
            "thread_summary_message_count_hint",
            "thread_summary_entity_name",
        }
        assert set(lifecycle["response_envelope"]) == {
            "source_event_id",
            "target",
            "body",
            "attachment_ids",
            "mentioned_agents",
            "agent_name",
            "origin",
            "hook_source",
            "message_received_depth",
            "dispatch_policy_source_kind",
        }
        assert set(lifecycle["response_envelope"]["origin"]) == {
            "transport_sender_id",
            "requester_id",
            "sender_entity_name",
            "requester_entity_name",
            "sender_kind",
            "requester_kind",
            "intent",
            "source_kind",
            "trust",
        }


def _worker(
    store: TerminalDeliveryStore,
    attempt: Callable[[PendingTerminalDelivery], Awaitable[TerminalDeliveryAttempt]],
    *,
    clock: _Clock,
    max_concurrency: int = 4,
    is_ready: Callable[[], bool] | None = None,
    poll_interval_seconds: float = 600.0,
    complete_lifecycle: Callable[[PendingTerminalDelivery], Awaitable[None]] | None = None,
) -> TerminalDeliveryWorker:
    async def noop_complete_lifecycle(_item: PendingTerminalDelivery) -> None:
        return

    return TerminalDeliveryWorker(
        TerminalDeliveryWorkerDeps(
            store=store,
            attempt=attempt,
            complete_lifecycle=complete_lifecycle or noop_complete_lifecycle,
            is_ready=is_ready or (lambda: True),
            logger=MagicMock(),
            wall_clock=clock,
            jitter=lambda: 1.0,
            poll_interval_seconds=poll_interval_seconds,
            max_concurrency=max_concurrency,
            initial_backoff_seconds=1.0,
            max_backoff_seconds=8.0,
        ),
    )


async def _wait_until_discarded(store: TerminalDeliveryStore, delivery_id: str) -> None:
    """Wait until one durable record leaves the outbox, which means its outcome landed.

    The store has no completion signal to await, so this polls it; the surrounding
    timeout keeps a regression from hanging the suite.
    """
    async with asyncio.timeout(_STATE_WAIT_TIMEOUT_SECONDS):
        while store.get(delivery_id) is not None:  # noqa: ASYNC110
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

        # A delivered row is dropped outright rather than retained as history.
        assert store.get(recorded.delivery_id) is None
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

    def test_backoff_clamps_before_exponentiating(self, tmp_path: Path) -> None:
        """Corrupt or ancient attempt counts cannot overflow backoff calculation."""
        clock = _Clock()
        worker = _worker(_store(tmp_path, clock), AsyncMock(), clock=clock)

        assert worker._backoff_seconds(1_000_000) == 8.0

    @pytest.mark.asyncio
    async def test_a_committed_answer_is_never_abandoned_for_taking_too_long(self, tmp_path: Path) -> None:
        """A long outage must not discard the outcome; retries continue at capped backoff."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        recorded = store.record(_intent())
        assert recorded is not None

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.transient("edit_failed")

        worker = _worker(store, attempt, clock=clock)
        for _round in range(40):
            await worker.drain_once()
            item = store.get(recorded.delivery_id)
            assert item is not None
            clock.advance(max(0.0, item.next_attempt_at - clock.now))

        still_owed = store.get(recorded.delivery_id)
        assert still_owed is not None
        assert still_owed.state == "retry_wait"
        assert still_owed.attempts == 40
        assert store.pending_target_event_ids() == frozenset({PLACEHOLDER_EVENT_ID})

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

        assert store.get(recorded.delivery_id) is None
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
    async def test_same_room_waiters_do_not_consume_global_slots(self, tmp_path: Path) -> None:
        """Queued work for one room cannot starve an independent room."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        for index in range(3):
            assert (
                store.record(
                    _intent(
                        room_id="!busy:localhost",
                        source_event_id=f"$busy-{index}",
                        target_event_id=f"$busy-placeholder-{index}",
                    ),
                )
                is not None
            )
            clock.advance(0.001)
        assert (
            store.record(
                _intent(
                    room_id="!other:localhost",
                    source_event_id="$other",
                    target_event_id="$other-placeholder",
                ),
            )
            is not None
        )
        busy_started = asyncio.Event()
        release_busy = asyncio.Event()
        other_started = asyncio.Event()

        async def attempt(item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            if item.target.room_id == "!busy:localhost" and not busy_started.is_set():
                busy_started.set()
                await release_busy.wait()
            if item.target.room_id == "!other:localhost":
                other_started.set()
            return TerminalDeliveryAttempt.delivered_now()

        drain = asyncio.create_task(_worker(store, attempt, clock=clock, max_concurrency=2).drain_once())
        await busy_started.wait()
        try:
            async with asyncio.timeout(0.1):
                await other_started.wait()
        finally:
            release_busy.set()
            await drain

    @pytest.mark.asyncio
    async def test_worker_does_not_spin_while_a_due_row_waits_for_readiness(self, tmp_path: Path) -> None:
        """Startup leaves warmed rows due; the loop must park instead of busy-waiting."""
        clock = _Clock()
        store = _store(tmp_path, clock)
        assert store.record(_intent()) is not None
        iterations = 0

        async def attempt(_item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
            return TerminalDeliveryAttempt.delivered_now()

        worker = _worker(store, attempt, clock=clock, is_ready=lambda: False)
        original_wait = worker._wait_for_work

        async def counted_wait() -> None:
            nonlocal iterations
            iterations += 1
            await original_wait()

        worker._wait_for_work = counted_wait
        worker.start()
        try:
            await asyncio.sleep(0.2)
        finally:
            await worker.stop()

        assert iterations <= 2

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
            await _wait_until_discarded(store, recorded.delivery_id)
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
            await _wait_until_discarded(store, recorded.delivery_id)
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


def _identity(
    target: MessageTarget,
    *,
    correlation_id: str = "corr-durable",
    source_event_ids: tuple[str, ...] = (SOURCE_EVENT_ID,),
) -> ResponseIdentity:
    return ResponseIdentity(
        response_kind="ai",
        response_envelope=_envelope(target),
        correlation_id=correlation_id,
        source_event_ids=source_event_ids,
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
    resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(
        return_value=target.resolved_thread_id,
    )
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
    async def test_pending_delivery_persists_every_coalesced_source_event(self, tmp_path: Path) -> None:
        """Redacting any source in a coalesced prompt must cancel its pending answer."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        assert store is not None

        await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target, source_event_ids=(SOURCE_EVENT_ID, "$source-2", "$source-3")),
            body=FINAL_BODY,
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )

        [pending] = store.unsettled_items()
        assert pending.source_event_ids == (SOURCE_EVENT_ID, "$source-2", "$source-3")

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
    async def test_recording_still_repairs_the_visible_placeholder(self, tmp_path: Path) -> None:
        """Recording does not classify, so the placeholder must never be left spinning."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        edits: list[str] = []

        async def room_send(**kwargs: object) -> object:
            content = kwargs["content"]
            assert isinstance(content, dict)
            new_content = content.get("m.new_content")
            if isinstance(new_content, dict):
                edits.append(str(new_content.get("body")))
            # Permanent rejection: the payload can never be delivered as-is.
            return nio.RoomSendError.from_dict({"errcode": "M_TOO_LARGE", "error": "too large"}, ROOM_ID)

        client.room_send = AsyncMock(side_effect=room_send)
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

        # The row is queued for repair, and the user still sees a failure note
        # rather than a placeholder that spins forever.
        assert len(store.unsettled_items()) == 1
        assert outcome.failure_reason == _DURABLE_TERMINAL_RETRY_FAILURE_REASON
        assert any("Response delivery failed" in body for body in edits)

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
    async def test_restart_repair_emits_success_hook_once(self, tmp_path: Path) -> None:
        """Repair must finish normal response lifecycle instead of only editing Matrix."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        first_gateway, first_store = _gateway(
            tmp_path=tmp_path,
            client=make_matrix_client_mock(),
            target=target,
        )
        await first_gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target),
            body=FINAL_BODY,
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )
        assert first_store is not None

        _reset_terminal_delivery_store_runtime()
        recovered_client = make_matrix_client_mock()
        recovered_client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repair-edit"}, ROOM_ID),
        )
        restarted_gateway, restarted_store = _gateway(
            tmp_path=tmp_path,
            client=recovered_client,
            target=target,
        )
        assert restarted_store is not None
        restarted_store.warm()

        lifecycle = TerminalDeliveryLifecycleReplayer(
            TerminalDeliveryLifecycleReplayerDeps(
                response_hooks=restarted_gateway.deps.response_hooks,
                post_response_effects=PostResponseEffectsSupport(
                    runtime=restarted_gateway.deps.runtime,
                    logger=restarted_gateway.deps.logger,
                    runtime_paths=restarted_gateway.deps.runtime_paths,
                    delivery_gateway=restarted_gateway,
                    conversation_cache=restarted_gateway.deps.resolver.deps.conversation_cache,
                ),
                logger=restarted_gateway.deps.logger,
            ),
        )
        worker = _worker(
            restarted_store,
            restarted_gateway.attempt_pending_terminal_delivery,
            clock=_Clock(),
            complete_lifecycle=lifecycle.complete,
        )

        assert await worker.drain_once() == 1
        restarted_gateway.deps.response_hooks.emit_after_response.assert_awaited_once_with(
            identity=_identity(target),
            response_text=FINAL_BODY,
            response_event_id=PLACEHOLDER_EVENT_ID,
            delivery_kind="edited",
            continue_on_cancelled=True,
        )
        assert restarted_store.items() == ()

    @pytest.mark.asyncio
    async def test_restart_repair_replays_interactive_and_thread_summary_effects_once(
        self,
        tmp_path: Path,
    ) -> None:
        """Restart repair retains ordinary successful post-response obligations."""
        target = MessageTarget.resolve(ROOM_ID, "$thread", SOURCE_EVENT_ID)
        metadata = InteractiveMetadata.from_parts(
            {"1": "approve"},
            [{"key": "1", "label": "Approve"}],
            question_text="Approve?",
        )
        assert metadata is not None
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        assert store is not None
        await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(target),
                correlation_id="corr-durable",
                source_event_ids=(SOURCE_EVENT_ID, "$source-2"),
                thread_summary_message_count_hint=7,
            ),
            body="Approve?\n\nReact with an emoji or type the number to respond.",
            tool_trace=None,
            extra_content=None,
            interactive_metadata=metadata,
        )

        _reset_terminal_delivery_store_runtime()
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repair-edit"}, ROOM_ID),
        )
        restarted_gateway, restarted_store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert restarted_store is not None
        restarted_store.warm()
        restarted_gateway.deps.resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed.return_value = (
            "$latest"
        )
        effects = _PostEffectRecorder()
        lifecycle = TerminalDeliveryLifecycleReplayer(
            TerminalDeliveryLifecycleReplayerDeps(
                response_hooks=restarted_gateway.deps.response_hooks,
                post_response_effects=effects,
                logger=restarted_gateway.deps.logger,
            ),
        )
        worker = _worker(
            restarted_store,
            restarted_gateway.attempt_pending_terminal_delivery,
            clock=_Clock(restarted_store.clock()),
            complete_lifecycle=lifecycle.complete,
        )

        assert await worker.drain_once() == 1
        _reset_terminal_delivery_store_runtime()
        assert _store(tmp_path).warm() == ()
        assert effects.interactive == [(PLACEHOLDER_EVENT_ID, target, metadata)]
        assert effects.thread_summaries == [(ROOM_ID, "$thread", "helper")]

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
    async def test_oversized_retry_reuses_persisted_prepared_wire_payload(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An oversized durable edit uploads once, then restart retries exact frozen bytes."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse.from_dict({"event_id": "$sent"}, ROOM_ID))
        upload_sidecar = AsyncMock(
            return_value=(
                "mxc://localhost/frozen-sidecar",
                {"size": 100_000, "mimetype": "application/json"},
            ),
        )
        monkeypatch.setattr("mindroom.matrix.large_messages.upload_json_sidecar", upload_sidecar)
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert store is not None
        recorded = await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target),
            body="large answer " + ("x" * 40_000),
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )
        assert recorded is not None

        _reset_terminal_delivery_store_runtime()
        restarted_gateway, restarted_store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert restarted_store is not None
        restarted_store.warm()
        item = restarted_store.unsettled_items()[0]
        first = await restarted_gateway.attempt_pending_terminal_delivery(item)
        second = await restarted_gateway.attempt_pending_terminal_delivery(item)

        assert (first.result, second.result) == ("delivered", "delivered")
        assert upload_sidecar.await_count == 1
        assert (
            len({json.dumps(call.kwargs["content"], sort_keys=True) for call in client.room_send.await_args_list}) == 1
        )
        assert {call.kwargs["tx_id"] for call in client.room_send.await_args_list} == {item.transaction_id}

    @pytest.mark.asyncio
    async def test_stale_revision_never_reaches_matrix(self, tmp_path: Path) -> None:
        """A replacement committed before an old attempt prevents the old body from sending."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse.from_dict({"event_id": "$sent"}, ROOM_ID))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert store is not None
        old = await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target, correlation_id="corr-old"),
            body="old body",
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )
        replacement = await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target, correlation_id="corr-new"),
            body="new body",
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )
        assert old is not None
        assert replacement is not None

        attempt = await gateway.attempt_pending_terminal_delivery(old)

        assert (attempt.result, attempt.reason) == ("superseded", "stale_revision")
        client.room_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replacement_waits_for_in_flight_revision(self, tmp_path: Path) -> None:
        """Replacement recording and sending one target are serialized."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        inspection_started = asyncio.Event()
        release_inspection = asyncio.Event()

        async def inspect_target(_room_id: str, _event_id: str) -> object:
            inspection_started.set()
            await release_inspection.wait()
            return nio.RoomGetEventResponse.from_dict(
                {
                    "event_id": PLACEHOLDER_EVENT_ID,
                    "sender": "@helper:localhost",
                    "origin_server_ts": 1,
                    "type": "m.room.message",
                    "room_id": ROOM_ID,
                    "content": {"msgtype": "m.text", "body": "placeholder"},
                },
            )

        client.room_get_event = AsyncMock(side_effect=inspect_target)
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse.from_dict({"event_id": "$sent"}, ROOM_ID))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        assert store is not None
        old = await gateway.record_pending_terminal_delivery(
            target=target,
            target_event_id=PLACEHOLDER_EVENT_ID,
            identity=_identity(target, correlation_id="corr-old"),
            body="old body",
            tool_trace=None,
            extra_content=None,
            interactive_metadata=None,
        )
        assert old is not None

        attempt_task = asyncio.create_task(gateway.attempt_pending_terminal_delivery(old))
        await inspection_started.wait()
        replacement_task = asyncio.create_task(
            gateway.record_pending_terminal_delivery(
                target=target,
                target_event_id=PLACEHOLDER_EVENT_ID,
                identity=_identity(target, correlation_id="corr-new"),
                body="new body",
                tool_trace=None,
                extra_content=None,
                interactive_metadata=None,
            ),
        )
        await asyncio.sleep(0)
        assert not replacement_task.done()

        release_inspection.set()
        attempt = await attempt_task
        replacement = await replacement_task

        assert attempt.result == "delivered"
        assert replacement is not None
        assert replacement.revision == old.revision + 1

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
        assert store.items() == ()

    @pytest.mark.asyncio
    async def test_store_failure_still_sanitizes_conversation_cache(self) -> None:
        """Durable cancellation errors propagate only after advisory cache cleanup."""

        class FailingTerminalStore:
            def supersede_sources(self, _source_event_ids: object, *, reason: str) -> None:
                assert reason == "source_event_redacted"
                error = OSError("durable store unavailable")
                raise error

        class TurnStoreStub:
            def mark_source_redacted(self, source_event_id: str) -> None:
                assert source_event_id == SOURCE_EVENT_ID

        class ConversationCacheStub:
            def __init__(self) -> None:
                self.redacted: list[str] = []

            async def apply_redaction(self, _room: str, event: nio.RedactionEvent) -> None:
                self.redacted.append(event.redacts)

        conversation_cache = ConversationCacheStub()
        cleanup = RedactedTurnCleanup(
            RedactedTurnCleanupDeps(
                conversation_cache=conversation_cache,  # type: ignore[arg-type]
                turn_store=TurnStoreStub(),  # type: ignore[arg-type]
                terminal_delivery_store=FailingTerminalStore(),  # type: ignore[arg-type]
            ),
        )
        room = MagicMock()
        room.room_id = ROOM_ID
        event = raw_nio_redaction(
            {
                "type": "m.room.redaction",
                "event_id": "$redaction",
                "sender": "@user:localhost",
                "origin_server_ts": 1,
            },
            redacts=SOURCE_EVENT_ID,
        )

        with pytest.raises(OSError, match="durable store unavailable"):
            await cleanup.handle(room, event)

        assert conversation_cache.redacted == [SOURCE_EVENT_ID]

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
