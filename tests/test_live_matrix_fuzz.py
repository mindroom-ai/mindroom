"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.testing.fuzz_live_matrix import (
    ExactReplyOracle,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    live_scenario_from_seed,
    recovery_scenario_from_seed,
    saturation_scenario,
)

LIMITED_SYNC_REPRODUCER = Path(__file__).parent / "fixtures" / "matrix_fuzz" / "limited_sync_concurrent_branch.json"


def test_live_scenario_is_deterministic_and_json_replayable() -> None:
    """A seed must produce a stable trace that survives JSON round-tripping."""
    scenario = live_scenario_from_seed(
        42,
        steps=250,
        thread_count=12,
        max_batch_size=10,
        restart_interval=75,
    )

    assert scenario == live_scenario_from_seed(
        42,
        steps=250,
        thread_count=12,
        max_batch_size=10,
        restart_interval=75,
    )
    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario
    assert (
        sum(
            operation.kind is not LiveOperationKind.RESTART_MINDROOM
            for batch in scenario.batches
            for operation in batch
        )
        == 250
    )
    assert any(
        operation.kind is LiveOperationKind.RESTART_MINDROOM for batch in scenario.batches for operation in batch
    )
    for batch in scenario.batches:
        reply_threads = [
            operation.thread
            for operation in batch
            if operation.kind
            in {
                LiveOperationKind.THREAD_MESSAGE,
                LiveOperationKind.PLAIN_REPLY,
            }
        ]
        assert len(reply_threads) == len(set(reply_threads))


def test_live_scenario_generator_covers_every_matrix_mutation() -> None:
    """The weighted generator must reach every supported live operation."""
    seen = {
        operation.kind
        for seed in range(5)
        for batch in live_scenario_from_seed(
            seed,
            steps=200,
            thread_count=8,
            restart_interval=50,
        ).batches
        for operation in batch
    }

    assert seen == set(LiveOperationKind)


def test_saturation_scenario_matches_original_two_phase_workload() -> None:
    """The regression profile must preserve the old hot-then-parallel ordering."""
    scenario = saturation_scenario()

    assert scenario.thread_count == 13
    assert len(scenario.batches) == 108
    assert all(len(batch) == 1 and batch[0].thread == 0 for batch in scenario.batches[:100])
    assert all([operation.thread for operation in batch] == list(range(1, 13)) for batch in scenario.batches[100:])


def test_recovery_scenario_is_replayable_and_forces_every_room_past_sync_limit() -> None:
    """The outage trace fixes room, sender, thread, transaction, and retry scheduling."""
    scenario = recovery_scenario_from_seed(
        1638,
        messages_per_room=51,
        room_count=2,
        thread_count=6,
        client_count=9,
        max_batch_size=6,
    )

    assert scenario == recovery_scenario_from_seed(
        1638,
        messages_per_room=51,
        room_count=2,
        thread_count=6,
        client_count=9,
        max_batch_size=6,
    )
    assert LiveFuzzScenario.from_json(scenario.to_json()) == scenario
    messages_by_room = {
        room: sum(
            operation.kind is not LiveOperationKind.IDEMPOTENT_RETRY and operation.room == room
            for batch in scenario.batches
            for operation in batch
        )
        for room in range(scenario.room_count)
    }
    assert messages_by_room == {0: 51, 1: 51}
    assert any(
        operation.kind is LiveOperationKind.IDEMPOTENT_RETRY for batch in scenario.batches for operation in batch
    )
    for batch in scenario.batches:
        reply_threads = [
            (operation.room, operation.thread)
            for operation in batch
            if operation.kind
            in {
                LiveOperationKind.THREAD_MESSAGE,
                LiveOperationKind.PLAIN_REPLY,
            }
        ]
        assert len(reply_threads) == len(set(reply_threads))


def test_limited_sync_concurrent_reproducer_remains_an_exact_seeded_trace() -> None:
    """Keep the minimized limited-sync concurrency trace replayable."""
    saved = LiveFuzzScenario.from_json(LIMITED_SYNC_REPRODUCER.read_text(encoding="utf-8"))
    generated = recovery_scenario_from_seed(
        1638,
        messages_per_room=51,
        room_count=1,
        thread_count=12,
        client_count=6,
        max_batch_size=12,
    )

    assert saved == generated
    assert sum(len(batch) for batch in saved.batches) == 57


def test_live_scenario_rejects_same_batch_dependency() -> None:
    """Concurrent operations may only target events from completed batches."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.REACTION, 0, "op:0"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="unknown or same-batch target"):
        scenario.validate()


def test_live_scenario_rejects_ambiguous_same_thread_reply_batch() -> None:
    """The exact-reply oracle cannot distinguish a valid coalesced turn from loss."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        batches=(
            (
                LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0"),
                LiveOperation(1, LiveOperationKind.PLAIN_REPLY, 0, "response:root:0"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="same-thread messages"):
        scenario.validate()


def test_live_scenario_rejects_cross_room_dependencies() -> None:
    """A room-local relation may not point at another recovery room's event."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        room_count=2,
        client_count=1,
        profile="recovery",
        batches=(
            (
                LiveOperation(
                    0,
                    LiveOperationKind.THREAD_MESSAGE,
                    0,
                    "root:0:0",
                    room=1,
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="cross-room target"):
        scenario.validate()


def test_recovery_scenario_rejects_reused_coalescing_lane() -> None:
    """Outage sources sharing a sender and thread cannot have one-reply-per-source semantics."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        client_count=1,
        profile="recovery",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0:0"),),
            (LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0:0"),),
        ),
    )

    with pytest.raises(ValueError, match="intentional coalescing"):
        scenario.validate()


@pytest.mark.asyncio
async def test_exact_reply_oracle_counts_only_canonical_agent_thread_replies() -> None:
    """Edits and duplicate sync delivery must not inflate canonical counts."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")

    canonical: dict[str, Any] = {
        "event_id": "$response",
        "sender": "@agent:example",
        "type": "m.room.message",
        "content": {
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$source",
                "m.in_reply_to": {"event_id": "$source"},
            },
        },
    }
    oracle._ingest_event(canonical)
    oracle._ingest_event(canonical)
    oracle._ingest_event(
        {
            **canonical,
            "event_id": "$edit",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$response",
                },
            },
        },
    )

    assert oracle.response_ids == {"$source": {"$response"}}
    assert oracle.resolve_response_ref("response:root:0") == "$response"
    oracle._assert_no_wrong_replies()
    await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_hydrates_limited_sync_from_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated observer sync must hydrate history without weakening exact counts."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    canonical: dict[str, Any] = {
        "event_id": "$response",
        "sender": "@agent:example",
        "type": "m.room.message",
        "content": {
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$source",
                "m.in_reply_to": {"event_id": "$source"},
            },
        },
    }

    async def sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms == 0
        return {
            "next_batch": "sync-token",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": True,
                            "events": [canonical],
                        },
                    },
                },
            },
        }

    async def messages_before(
        from_position: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        assert from_position == "sync-token"
        assert to_token is None
        assert limit == 1000
        return [canonical], None

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await oracle._sync_once(timeout_ms=0, allow_limited=True)
    finally:
        await client.close()

    assert oracle.response_ids == {"$source": {"$response"}}
    assert oracle.limited_timeline_count == 1
    assert oracle.pagination_page_count == 1


@pytest.mark.asyncio
async def test_exact_reply_oracle_audits_bounded_limited_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The independent gap audit must expose a server-boundary source omission."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    oracle.next_batch = "since-position"

    async def sync(_since: str | None, *, timeout_ms: int) -> dict[str, Any]:
        assert timeout_ms == 0
        return {
            "next_batch": "sync-position",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": True,
                            "prev_batch": "gap-position",
                            "events": [],
                        },
                    },
                },
            },
        }

    async def messages_before(
        from_position: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        assert limit == 1000
        if to_token is not None:
            assert (from_position, to_token) == ("gap-position", "since-position")
            return [], None
        assert from_position == "sync-position"
        return [
            {
                "event_id": "$source",
                "sender": "@user:example",
                "type": "m.room.message",
                "content": {},
            },
        ], None

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await oracle._sync_once(timeout_ms=0, allow_limited=True)
    finally:
        await client.close()

    assert oracle.gap_audit_missing_sources == {"$source"}
    assert oracle.gap_audit_page_count == 1
    assert oracle.pagination_page_count == 1


@pytest.mark.asyncio
async def test_exact_reply_oracle_rejects_duplicate_canonical_replies() -> None:
    """Two distinct agent events replying to one input must fail immediately."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    for event_id in ("$response-one", "$response-two"):
        oracle._ingest_event(
            {
                "event_id": event_id,
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$source",
                        "m.in_reply_to": {"event_id": "$source"},
                    },
                },
            },
        )

    with pytest.raises(AssertionError, match="duplicates"):
        oracle._assert_no_wrong_replies()
    await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_allows_response_to_internal_restart_relay() -> None:
    """Restart recovery may validly answer a router-authored resume relay."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(
        client,
        "@agent:example",
        internal_relay_senders=("@router:example",),
    )
    try:
        oracle._ingest_event(
            {
                "event_id": "$resume-relay",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {"body": "resume"},
            },
        )
        oracle._ingest_event(
            {
                "event_id": "$response",
                "sender": "@agent:example",
                "type": "m.room.message",
                "content": {
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$root",
                        "m.in_reply_to": {"event_id": "$resume-relay"},
                    },
                },
            },
        )
        oracle._assert_no_wrong_replies()
    finally:
        await client.close()
