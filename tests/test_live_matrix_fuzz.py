"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.testing import fuzz_live_matrix
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


def test_instance_registry_read_retries_a_partial_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live-server probe must tolerate the deployer's non-atomic write window."""

    class PartialRegistry:
        reads = 0

        @staticmethod
        def exists() -> bool:
            return True

        @classmethod
        def read_text(cls, *, encoding: str) -> str:
            assert encoding == "utf-8"
            cls.reads += 1
            return "{" if cls.reads == 1 else '{"instances": {}}'

    monkeypatch.setattr(fuzz_live_matrix, "INSTANCE_REGISTRY", PartialRegistry())
    monkeypatch.setattr(fuzz_live_matrix, "REGISTRY_READ_RETRY_SECONDS", 0)

    assert fuzz_live_matrix._active_fuzz_instances() == ()
    assert PartialRegistry.reads == 2


def test_instance_registry_read_fails_closed_when_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently malformed registry must not permit a duplicate fuzz stack."""
    registry = tmp_path / "instances.json"
    registry.write_text("{", encoding="utf-8")
    monkeypatch.setattr(fuzz_live_matrix, "INSTANCE_REGISTRY", registry)
    monkeypatch.setattr(fuzz_live_matrix, "REGISTRY_READ_RETRY_SECONDS", 0)

    with pytest.raises(RuntimeError, match="refusing to start"):
        fuzz_live_matrix._active_fuzz_instances()


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


def test_recovery_scenario_rejects_operation_the_runner_cannot_execute() -> None:
    """Recovery traces must fail validation before unsupported execution."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        client_count=1,
        profile="recovery",
        batches=((LiveOperation(0, LiveOperationKind.EDIT, 0, "root:0:0"),),),
    )

    with pytest.raises(ValueError, match="recovery profile does not support"):
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
    oracle.next_batch = "since-position"
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

    async def sync(
        _since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        assert timeout_ms == 0
        assert timeline_limit == 2000
        return {
            "next_batch": "sync-token",
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
        assert to_token == "since-position"  # noqa: S105 - opaque sync token
        assert limit == 1000
        if from_position == "gap-position":
            return [], "empty-page-token"
        assert from_position == "empty-page-token"
        return [canonical], None

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await oracle._sync_once(timeout_ms=0, allow_limited=True)
    finally:
        await client.close()

    assert oracle.response_ids == {"$source": {"$response"}}
    assert oracle.limited_timeline_count == 1
    assert oracle.pagination_page_count == 2


def _agent_reply_event(source_event_id: str, response_event_id: str, body: str) -> dict[str, Any]:
    return {
        "event_id": response_event_id,
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": 100,
        "content": {
            "body": body,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": source_event_id,
                "m.in_reply_to": {"event_id": source_event_id},
            },
        },
    }


def _agent_edit_event(response_event_id: str, body: str) -> dict[str, Any]:
    return {
        "event_id": "$edit",
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": 101,
        "content": {
            "body": f" * {body}",
            "m.new_content": {"body": body, "msgtype": "m.text"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": response_event_id},
        },
    }


@pytest.mark.asyncio
async def test_exact_reply_oracle_requires_completed_streaming_body() -> None:
    """A placeholder original is not complete until its final edit arrives."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("root:0", "$source")
    try:
        oracle._ingest_event(_agent_reply_event("$source", "$response", "Thinking..."))
        assert oracle._incomplete_streaming_sources() == {"$source"}

        body = fuzz_live_matrix._ModelHandler.response_text_for(7)
        oracle._ingest_event(_agent_edit_event("$response", body))

        assert oracle._incomplete_streaming_sources() == set()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_restart_barrier_keeps_duplicate_audit_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed old duplicate must be seen before the restart barrier settles."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("old", "$old-source")
    oracle._ingest_event(
        _agent_reply_event(
            "$old-source",
            "$old-response",
            fuzz_live_matrix._ModelHandler.response_text_for(1),
        ),
    )
    oracle.expect("restart-barrier:0", "$barrier-source")
    responses = iter(
        (
            [],
            [
                _agent_reply_event(
                    "$old-source",
                    "$delayed-duplicate",
                    fuzz_live_matrix._ModelHandler.response_text_for(2),
                ),
            ],
            [
                _agent_reply_event(
                    "$barrier-source",
                    "$barrier-response",
                    fuzz_live_matrix._ModelHandler.response_text_for(3),
                ),
            ],
        ),
    )

    async def sync(
        _since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        assert timeout_ms == 250
        assert timeline_limit == 2000
        return {
            "next_batch": f"token-{len(oracle.seen_event_ids)}",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": False,
                            "events": next(responses),
                        },
                    },
                },
            },
        }

    monkeypatch.setattr(client, "sync", sync)
    try:
        with pytest.raises(AssertionError, match="duplicates"):
            await oracle.wait_until_exact(deadline_seconds=1, settle_seconds=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_does_not_paginate_initial_limited_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization has no expected sources and must not walk all room history."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")

    async def sync(
        _since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        assert timeout_ms == 0
        assert timeline_limit == 2000
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
        _from_position: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        pytest.fail(f"unexpected pagination to {to_token=} with {limit=}")

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await oracle.initialize()
    finally:
        await client.close()

    assert oracle.limited_timeline_count == 1
    assert oracle.pagination_page_count == 0


@pytest.mark.asyncio
async def test_exact_reply_oracle_audits_bounded_limited_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The independent gap audit must expose a server-boundary source omission."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("old-root", "$old-source")
    oracle.next_batch = "since-position"
    oracle.arm_gap_audit()
    oracle.expect("root:0", "$source")

    async def sync(
        _since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        assert timeout_ms == 0
        assert timeline_limit == 50
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
        assert (from_position, to_token) == ("gap-position", "since-position")
        return [], None

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await oracle.audit_armed_limited_gap()
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
