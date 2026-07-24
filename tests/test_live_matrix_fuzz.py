"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from mindroom.matrix.sync_tokens import save_sync_token
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


def _recovery_scenario_with_sources(
    source_count: int,
    *,
    client_count: int = 1,
) -> LiveFuzzScenario:
    return LiveFuzzScenario(
        thread_count=max(source_count, 1),
        room_count=1,
        client_count=client_count,
        profile="recovery",
        batches=tuple(
            (
                LiveOperation(
                    operation_id,
                    LiveOperationKind.THREAD_MESSAGE,
                    operation_id,
                    f"root:0:{operation_id}",
                    room=0,
                    client=0,
                ),
            )
            for operation_id in range(source_count)
        ),
    )


def test_exact_nio_provenance_fails_closed() -> None:
    """An exact campaign may not run against unverifiable or different nio."""
    provenance = fuzz_live_matrix.RuntimeProvenance(
        mindroom_module_path="/loaded/mindroom/__init__.py",
        mindroom_revision="mindroom-sha",
        mindroom_expected_revision="mindroom-sha",
        nio_module_path="/loaded/nio/__init__.py",
        nio_version="1.0",
        nio_revision="loaded-sha",
        nio_expected_revision="required-sha",
    )

    with pytest.raises(RuntimeError, match=r"required-sha.*loaded-sha"):
        fuzz_live_matrix._validate_nio_provenance(provenance)


def test_exact_nio_provenance_rejects_unverified_or_dirty_source() -> None:
    """A clean commit label must not conceal unverifiable or modified imports."""
    unverified = fuzz_live_matrix.RuntimeProvenance(
        mindroom_module_path="/loaded/mindroom/__init__.py",
        mindroom_revision="mindroom-sha",
        mindroom_expected_revision="mindroom-sha",
        nio_module_path="/loaded/nio/__init__.py",
        nio_version="1.0",
        nio_revision="unverified",
        nio_expected_revision="nio-sha",
    )
    dirty = replace(
        unverified,
        nio_revision="nio-sha",
        nio_expected_revision="nio-sha",
        nio_dirty=True,
    )

    with pytest.raises(RuntimeError, match="could not verify"):
        fuzz_live_matrix._validate_nio_provenance(unverified)
    with pytest.raises(RuntimeError, match="clean loaded nio source"):
        fuzz_live_matrix._validate_nio_provenance(dirty)


def test_non_exact_nio_provenance_accepts_wheel_hash() -> None:
    """Ordinary fuzz runs may use an unhashed wheel while still recording its source hash."""
    provenance = fuzz_live_matrix.RuntimeProvenance(
        mindroom_module_path="/loaded/mindroom/__init__.py",
        mindroom_revision="mindroom-sha",
        mindroom_expected_revision="mindroom-sha",
        nio_module_path="/site-packages/nio/__init__.py",
        nio_version="1.0",
        nio_revision="unverified",
        nio_expected_revision="",
        nio_dirty=True,
        nio_source_hash="content-hash",
    )

    fuzz_live_matrix._validate_nio_provenance(provenance)


def test_failure_artifact_includes_loaded_code_provenance(tmp_path: Path) -> None:
    """Failure JSON must identify both loaded repositories and exact trace."""
    provenance = fuzz_live_matrix.RuntimeProvenance(
        mindroom_module_path="/loaded/mindroom/__init__.py",
        mindroom_revision="mindroom-sha",
        mindroom_expected_revision="mindroom-sha",
        nio_module_path="/loaded/nio/__init__.py",
        nio_version="1.0",
        nio_revision="nio-sha",
        nio_expected_revision="nio-sha",
    )

    class ArtifactStack:
        log_path = tmp_path / "mindroom.log"

        @staticmethod
        def diagnostic_counts() -> dict[str, int]:
            return {"event_loop_stalls": 0}

    ArtifactStack.log_path.write_text("runtime output", encoding="utf-8")
    artifact = fuzz_live_matrix._failure_artifact(
        error=AssertionError("boom"),
        scenario=live_scenario_from_seed(1, steps=1, thread_count=1, restart_interval=0),
        seed=1,
        provenance=provenance,
        stack=cast("fuzz_live_matrix.ManagedTuwunelStack", ArtifactStack()),
        runtime_ms=123,
    )

    assert artifact["mindroom_revision"] == "mindroom-sha"
    assert artifact["mindroom_module_path"] == "/loaded/mindroom/__init__.py"
    assert artifact["nio_module_path"] == "/loaded/nio/__init__.py"
    assert artifact["nio_version"] == "1.0"
    assert artifact["nio_revision"] == "nio-sha"
    assert artifact["scenario"]["version"] == 1
    assert artifact["mindroom_log"] == "runtime output"
    assert LiveFuzzScenario.from_json(json.dumps(artifact)) == live_scenario_from_seed(
        1,
        steps=1,
        thread_count=1,
        restart_interval=0,
    )


def test_runtime_attestation_retains_child_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup failures after child import must still report the loaded runtime."""
    expected = fuzz_live_matrix.RuntimeProvenance(
        mindroom_module_path="/child/mindroom/__init__.py",
        mindroom_revision="mindroom-sha",
        mindroom_expected_revision="mindroom-sha",
        nio_module_path="/child/nio/__init__.py",
        nio_version="1.0",
        nio_revision="nio-sha",
        nio_expected_revision="nio-sha",
    )
    stack = object.__new__(fuzz_live_matrix.ManagedTuwunelStack)
    stack.attestation_path = tmp_path / "runtime-attestation.json"
    stack.attestation_path.write_text(
        json.dumps(
            {
                "mindroom_module_path": expected.mindroom_module_path,
                "nio_module_path": expected.nio_module_path,
            },
        ),
        encoding="utf-8",
    )
    stack.runtime_provenance = None
    stack._mindroom_process = None

    def provenance(
        *,
        mindroom_module_path: Path,
        nio_module_path: Path,
    ) -> fuzz_live_matrix.RuntimeProvenance:
        assert str(mindroom_module_path) == expected.mindroom_module_path
        assert str(nio_module_path) == expected.nio_module_path
        return expected

    monkeypatch.setattr(fuzz_live_matrix, "_runtime_provenance", provenance)

    stack._wait_for_runtime_attestation()

    assert stack.runtime_provenance is expected


@pytest.mark.asyncio
async def test_recovery_checkpoint_barrier_waits_for_durable_advance(
    tmp_path: Path,
) -> None:
    """The outage cannot start from a stale or absent agent checkpoint."""
    stack = object.__new__(fuzz_live_matrix.ManagedTuwunelStack)
    stack.storage_path = tmp_path
    stack.log_path = tmp_path / "mindroom.log"
    stack._mindroom_process = None
    save_sync_token(
        tmp_path,
        fuzz_live_matrix.AGENT_NAME,
        "after-roots",
        cache_generation="generation",
    )

    async def advance() -> None:
        await asyncio.sleep(0)
        save_sync_token(
            tmp_path,
            fuzz_live_matrix.AGENT_NAME,
            "post-root-barrier",
            cache_generation="generation",
        )

    advance_task = asyncio.create_task(advance())
    checkpoint = await stack.wait_for_sync_checkpoint_advance(
        fuzz_live_matrix.AGENT_NAME,
        "after-roots",
        deadline_seconds=1,
    )
    await advance_task

    assert checkpoint == "post-root-barrier"


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


@pytest.mark.parametrize("missing_field", ["room", "client"])
def test_recovery_json_requires_explicit_ownership(missing_field: str) -> None:
    """Recovery replay may not silently change room or access-token ownership."""
    payload = json.loads(_recovery_scenario_with_sources(51).to_json())
    del payload["batches"][0][0][missing_field]

    with pytest.raises(TypeError, match=missing_field):
        LiveFuzzScenario.from_json(json.dumps(payload))


def test_non_recovery_profiles_reject_ignored_clients() -> None:
    """Generic and saturation runs may not declare clients the runner ignores."""
    scenario = LiveFuzzScenario(
        thread_count=1,
        client_count=2,
        batches=((LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0", client=1),),),
    )

    with pytest.raises(ValueError, match="exactly one declared room and client"):
        scenario.validate()


def test_recovery_requires_more_than_the_limited_timeline_per_room() -> None:
    """Loaded recovery traces must preserve the limited-sync precondition."""
    with pytest.raises(ValueError, match="more than 50"):
        _recovery_scenario_with_sources(50).validate()

    _recovery_scenario_with_sources(51).validate()


def test_recovery_retry_preserves_client_and_thread_ownership() -> None:
    """Transaction replay is scoped to the original access token and lane."""
    scenario = _recovery_scenario_with_sources(51, client_count=2)
    retry = LiveOperation(
        51,
        LiveOperationKind.IDEMPOTENT_RETRY,
        0,
        "op:0",
        room=0,
        client=1,
    )
    scenario = replace(scenario, batches=(*scenario.batches, (retry,)))

    with pytest.raises(ValueError, match="preserve the original"):
        scenario.validate()


def test_recovery_rejects_cross_thread_and_offline_response_targets() -> None:
    """Recovery relations must stay in-lane and target events available during outage."""
    scenario = _recovery_scenario_with_sources(51)
    cross_thread = replace(scenario.batches[1][0], target="root:0:0")
    invalid_cross_thread = replace(
        scenario,
        batches=(scenario.batches[0], (cross_thread,), *scenario.batches[2:]),
    )
    offline_response = replace(scenario.batches[1][0], target="response:op:0")
    invalid_offline_response = replace(
        scenario,
        batches=(scenario.batches[0], (offline_response,), *scenario.batches[2:]),
    )

    with pytest.raises(ValueError, match="belongs to thread"):
        invalid_cross_thread.validate()
    with pytest.raises(ValueError, match="unknown or same-batch target"):
        invalid_offline_response.validate()


@pytest.mark.parametrize(
    "kind",
    [
        LiveOperationKind.PLAIN_REPLY,
        LiveOperationKind.EDIT,
        LiveOperationKind.REACTION,
        LiveOperationKind.REDACTION,
        LiveOperationKind.IDEMPOTENT_RETRY,
        LiveOperationKind.RESTART_MINDROOM,
    ],
)
def test_saturation_rejects_unsupported_operations(kind: LiveOperationKind) -> None:
    """Saturation traces may contain only the turns the runner executes."""
    target = None if kind is LiveOperationKind.RESTART_MINDROOM else "response:root:0"
    scenario = LiveFuzzScenario(
        thread_count=2,
        profile="saturation",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "response:root:0"),),
            (LiveOperation(1, kind, 0, target),),
            (LiveOperation(2, LiveOperationKind.THREAD_MESSAGE, 1, "response:root:1"),),
        ),
    )

    with pytest.raises(ValueError, match="saturation profile does not support"):
        scenario.validate()


def test_saturation_rejects_incomplete_parallel_batches_and_wrong_targets() -> None:
    """Every parallel phase must cover all lanes and follow its serialized chain."""
    incomplete = LiveFuzzScenario(
        thread_count=3,
        profile="saturation",
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "response:root:0"),),
            (LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 1, "response:root:1"),),
        ),
    )
    wrong_target = replace(
        saturation_scenario(hot_turns=1, parallel_threads=2, parallel_turns=1),
        batches=(
            (LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "response:root:0"),),
            (
                LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 1, "response:root:1"),
                LiveOperation(2, LiveOperationKind.THREAD_MESSAGE, 2, "response:op:999"),
            ),
        ),
    )

    with pytest.raises(ValueError, match="every nonzero thread"):
        incomplete.validate()
    with pytest.raises(ValueError, match="must target"):
        wrong_target.validate()


@pytest.mark.parametrize(
    "batches",
    [
        (),
        ((LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "response:root:0"),),),
        ((LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 1, "response:root:1"),),),
    ],
)
def test_saturation_requires_both_hot_and_parallel_phases(
    batches: tuple[tuple[LiveOperation, ...], ...],
) -> None:
    """Degenerate saturation traces cannot satisfy the two-phase gate."""
    scenario = LiveFuzzScenario(
        thread_count=2,
        profile="saturation",
        batches=batches,
    )

    with pytest.raises(ValueError, match="one hot batch and one complete parallel batch"):
        scenario.validate()


@pytest.mark.parametrize(
    ("hot_turns", "parallel_threads", "parallel_turns"),
    [(0, 1, 1), (1, 0, 1), (1, 1, 0)],
)
def test_saturation_generator_requires_positive_dimensions(
    hot_turns: int,
    parallel_threads: int,
    parallel_turns: int,
) -> None:
    """Generator cannot emit a degenerate saturation profile."""
    with pytest.raises(ValueError, match="dimensions must all be positive"):
        saturation_scenario(
            hot_turns=hot_turns,
            parallel_threads=parallel_threads,
            parallel_turns=parallel_turns,
        )


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


def _agent_edit_event(
    response_event_id: str,
    body: str,
    *,
    event_id: str = "$edit",
    timestamp: int = 101,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "sender": "@agent:example",
        "type": "m.room.message",
        "origin_server_ts": timestamp,
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
async def test_equal_timestamp_edits_use_event_id_not_ingestion_order() -> None:
    """Backward pagination order must not change Matrix edit selection."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    try:
        original = _agent_reply_event("$source", "$response", "Thinking...")
        original["origin_server_ts"] = 101
        oracle._ingest_event(original)
        oracle._ingest_event(
            _agent_edit_event("$response", "newer", event_id="$edit-z", timestamp=101),
        )
        oracle._ingest_event(
            _agent_edit_event("$response", "older", event_id="$edit-a", timestamp=101),
        )

        assert oracle.latest_reply_bodies["$response"][2] == "newer"
        assert (
            fuzz_live_matrix.LiveFuzzRunner._latest_event_body(
                (
                    original,
                    _agent_edit_event("$response", "newer", event_id="$edit-z", timestamp=101),
                    _agent_edit_event("$response", "older", event_id="$edit-a", timestamp=101),
                ),
                "$response",
            )
            == "newer"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exact_reply_oracle_rejects_wrong_thread_root() -> None:
    """A direct reply match cannot conceal attachment to another thread."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.expect("op:1", "$source", root_event_id="$expected-root")
    event = _agent_reply_event(
        "$source",
        "$response",
        fuzz_live_matrix._ModelHandler.response_text_for(1),
    )
    event["content"]["m.relates_to"]["event_id"] = "$wrong-root"
    try:
        oracle._ingest_event(event)
        with pytest.raises(AssertionError, match="wrong_thread_roots"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()


def test_model_response_is_bound_to_source_and_ordered_history() -> None:
    """Edits and duplicate history rows must change the independent identity."""
    original = fuzz_live_matrix._source_identity("root:0", "original")
    edited = fuzz_live_matrix._source_identity("root:0", "edited")
    source, history = fuzz_live_matrix._ModelHandler._request_identity(
        {
            "messages": [
                {"content": f"first LIVE-SOURCE[{original}]"},
                {"content": f"duplicate LIVE-SOURCE[{original}]"},
                {"content": f"latest LIVE-SOURCE[{edited}]"},
            ],
        },
    )
    body = fuzz_live_matrix._ModelHandler.response_text_for(
        7,
        source_marker=source,
        history_fingerprint=history,
    )

    assert source == edited
    assert ExactReplyOracle._is_complete_model_body(
        body,
        expected_source_marker=edited,
        expected_history_fingerprint=fuzz_live_matrix._history_fingerprint((original, original, edited)),
    )
    assert not ExactReplyOracle._is_complete_model_body(
        body,
        expected_source_marker=original,
        expected_history_fingerprint=history,
    )
    assert not ExactReplyOracle._is_complete_model_body(
        body,
        expected_source_marker=edited,
        expected_history_fingerprint=fuzz_live_matrix._history_fingerprint((original, edited)),
    )


def test_live_model_tracks_edit_and_redaction_transitions() -> None:
    """Edit redaction restores the prior revision; source redaction removes history."""

    class Stack:
        agent_id = "@agent:example"

    client = cast("LiveMatrixClient", object())
    runner = fuzz_live_matrix.LiveFuzzRunner(
        cast("fuzz_live_matrix.ManagedTuwunelStack", Stack()),
        (client,),
        LiveFuzzScenario(thread_count=1, batches=()),
        reply_timeout=1,
        settle_seconds=0,
    )
    original_content = runner._message_content("Original", source_marker="root:0")
    original_identity = fuzz_live_matrix._source_marker_from_content(original_content)
    runner.event_ids["root:0"] = "$root"
    runner._expect_source(
        runner.oracle,
        "root:0",
        "$root",
        root_event_id="$root",
        room=0,
        thread=0,
        source_content=original_content,
    )

    edit = LiveOperation(1, LiveOperationKind.EDIT, 0, "root:0")
    edited_content = runner._message_content("Edited", source_marker="root:0")
    edited_identity = fuzz_live_matrix._source_marker_from_content(edited_content)
    runner._record_edit_revision(edit, edited_content)

    assert runner.oracle.expected_model_identity["$root"] == (
        edited_identity,
        fuzz_live_matrix._history_fingerprint((edited_identity,)),
    )

    runner._record_redaction(
        LiveOperation(2, LiveOperationKind.REDACTION, 0, edit.event_ref),
    )
    assert runner.oracle.expected_model_identity["$root"] == (
        original_identity,
        fuzz_live_matrix._history_fingerprint((original_identity,)),
    )

    runner._record_redaction(
        LiveOperation(3, LiveOperationKind.REDACTION, 0, "root:0"),
    )
    assert runner._history_identities((0, 0)) == ()


def test_exact_reply_oracle_rejects_malformed_visible_agent_original() -> None:
    """A visible non-threaded agent duplicate cannot disappear from the audit."""
    oracle = ExactReplyOracle(cast("LiveMatrixClient", object()), "@agent:example")
    oracle._ingest_event(
        {
            "event_id": "$malformed",
            "sender": "@agent:example",
            "type": "m.room.message",
            "content": {
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$source"},
                },
            },
        },
    )

    with pytest.raises(AssertionError, match="malformed"):
        oracle._assert_no_wrong_replies()


def test_saturation_final_audit_rejects_late_corrupt_edit() -> None:
    """A corrupt replacement arriving during quiescence must fail the gate."""
    source_marker = fuzz_live_matrix._source_identity("op:1", "source")
    history = fuzz_live_matrix._history_fingerprint((source_marker,))
    original = _agent_reply_event(
        "$source",
        "$response",
        fuzz_live_matrix._ModelHandler.response_text_for(
            1,
            source_marker=source_marker,
            history_fingerprint=history,
        ),
    )
    original["content"]["m.relates_to"]["event_id"] = "$root"
    corrupt_edit = _agent_edit_event(
        "$response",
        "corrupt",
        event_id="$edit",
        timestamp=2,
    )

    class Client:
        def __init__(self) -> None:
            self.seen_events = {
                "$response": original,
                "$edit": corrupt_edit,
            }

    class Stack:
        agent_id = "@agent:example"

    runner = fuzz_live_matrix.LiveFuzzRunner(
        cast("fuzz_live_matrix.ManagedTuwunelStack", Stack()),
        (cast("LiveMatrixClient", Client()),),
        LiveFuzzScenario(thread_count=2, batches=(), profile="saturation"),
        reply_timeout=1,
        settle_seconds=0,
    )
    expected = {
        "$source": fuzz_live_matrix._ExpectedSaturationReply(
            root_event_id="$root",
            source_marker=source_marker,
            history_fingerprint=history,
        ),
    }

    with pytest.raises(AssertionError, match="corrupt_bodies"):
        runner._assert_saturation_replies(expected)


@pytest.mark.parametrize(
    "body",
    [
        "LIVE-FUZZ call=1 source=op:1 history=0000000000000000 END call=1",
        "LIVE-FUZZ call=1 source=op:1 history=0000000000000000 segment-000 END call=2",
        ("LIVE-FUZZ call=1 source=op:1 history=0000000000000000 segment-000 END call=1 END call=1"),
    ],
)
def test_exact_model_body_rejects_corrupt_saturation_suffixes(body: str) -> None:
    """Terminator substrings alone cannot complete a saturation turn."""
    assert not ExactReplyOracle._is_complete_model_body(body)


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
async def test_pre_outage_checkpoint_wait_is_attached_to_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable checkpoint wait starts before and finishes after a concrete event."""

    class BarrierClient:
        room_slot = 0
        client_slot = 0
        room_id = "!room:example"
        sent = False

        async def send_event(
            self,
            _event_type: str,
            _txn_id: str,
            _content: dict[str, Any],
        ) -> str:
            self.sent = True
            return "$barrier"

    client = BarrierClient()

    class BarrierStack:
        agent_id = "@agent:example"
        router_id = "@router:example"
        waited = False

        @staticmethod
        def sync_checkpoint_token(_agent_name: str) -> str:
            return "before"

        async def wait_for_sync_checkpoint_advance(
            self,
            _agent_name: str,
            previous_token: str | None,
            *,
            deadline_seconds: float,
        ) -> str:
            assert previous_token == "before"  # noqa: S105 - opaque sync token
            assert deadline_seconds == 1
            assert client.sent
            self.waited = True
            return "after"

    stack = BarrierStack()
    runner = fuzz_live_matrix.LiveFuzzRunner(
        cast("fuzz_live_matrix.ManagedTuwunelStack", stack),
        (cast("LiveMatrixClient", client),),
        LiveFuzzScenario(thread_count=1, batches=(), profile="recovery"),
        reply_timeout=1,
        settle_seconds=0,
    )
    runner.event_ids["root:0:0"] = "$root"
    runner.response_event_ids["response:root:0:0"] = "$root-response"

    async def wait_until_exact(
        *,
        deadline_seconds: float,
        settle_seconds: float,
        allow_limited: bool = False,
    ) -> None:
        assert (deadline_seconds, settle_seconds, allow_limited) == (1, 0, False)

    monkeypatch.setattr(runner.oracle, "wait_until_exact", wait_until_exact)
    await runner._send_recovery_checkpoint_barrier((runner.oracle,))

    assert stack.waited
    assert runner.oracle.expected_sources["$barrier"] == "pre-outage-checkpoint-barrier"


@pytest.mark.asyncio
async def test_final_generic_restart_runs_a_liveness_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trace ending in restart cannot pass from already-satisfied expectations."""

    class RestartStack:
        agent_id = "@agent:example"
        router_id = "@router:example"
        restarts = 0

        def restart_mindroom(self) -> None:
            self.restarts += 1

    stack = RestartStack()
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    restart = LiveOperation(0, LiveOperationKind.RESTART_MINDROOM, 0, None)
    runner = fuzz_live_matrix.LiveFuzzRunner(
        cast("fuzz_live_matrix.ManagedTuwunelStack", stack),
        (client,),
        LiveFuzzScenario(thread_count=1, batches=((restart,),)),
        reply_timeout=1,
        settle_seconds=0,
    )
    barriers = 0

    async def send_barrier() -> None:
        nonlocal barriers
        barriers += 1

    async def wait_until_exact(
        *,
        deadline_seconds: float,
        settle_seconds: float,
        allow_limited: bool = False,
    ) -> None:
        assert (deadline_seconds, settle_seconds, allow_limited) == (1, 0, False)

    monkeypatch.setattr(runner, "_send_generic_restart_barrier", send_barrier)
    monkeypatch.setattr(runner.oracle, "wait_until_exact", wait_until_exact)
    try:
        await runner._run_batches(((restart,),))
    finally:
        await client.close()

    assert stack.restarts == 1
    assert barriers == 1


@pytest.mark.asyncio
async def test_recovery_restart_fences_every_sender_thread_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A room-level sentinel cannot replace per-sender conversation fences."""

    class LaneClient:
        room_slot = 0
        room_id = "!room:example"

        def __init__(self, client_slot: int) -> None:
            self.client_slot = client_slot
            self.sent: list[str] = []

        async def send_event(
            self,
            _event_type: str,
            txn_id: str,
            _content: dict[str, Any],
        ) -> str:
            self.sent.append(txn_id)
            return f"$barrier-{self.client_slot}"

    clients = (LaneClient(0), LaneClient(1))

    class LaneStack:
        agent_id = "@agent:example"
        router_id = "@router:example"

        @staticmethod
        def sync_checkpoint_token(_agent_name: str) -> str:
            return "before"

        @staticmethod
        async def wait_for_sync_checkpoint_advance(
            _agent_name: str,
            _previous_token: str | None,
            *,
            deadline_seconds: float,
        ) -> str:
            assert deadline_seconds == 1
            return "after"

    operations = (
        LiveOperation(0, LiveOperationKind.THREAD_MESSAGE, 0, "root:0:0", client=0),
        LiveOperation(1, LiveOperationKind.THREAD_MESSAGE, 0, "root:0:0", client=1),
    )
    runner = fuzz_live_matrix.LiveFuzzRunner(
        cast("fuzz_live_matrix.ManagedTuwunelStack", LaneStack()),
        cast("tuple[LiveMatrixClient, ...]", clients),
        LiveFuzzScenario(
            thread_count=1,
            client_count=2,
            profile="recovery",
            batches=tuple((operation,) for operation in operations),
        ),
        reply_timeout=1,
        settle_seconds=0,
    )
    runner.event_ids["root:0:0"] = "$root"
    for operation in operations:
        runner._latest_source_ref[(0, operation.client, 0)] = operation.event_ref
        runner.response_event_ids[f"response:{operation.event_ref}"] = f"$response-{operation.client}"

    async def wait_until_exact(
        *,
        deadline_seconds: float,
        settle_seconds: float,
        allow_limited: bool = False,
    ) -> None:
        assert deadline_seconds == 1
        assert settle_seconds in {0, 1}
        assert allow_limited

    monkeypatch.setattr(runner.oracle, "wait_until_exact", wait_until_exact)
    barrier_count = await runner._send_recovery_restart_barriers((runner.oracle,))

    assert barrier_count == 2
    assert all(len(client.sent) == 1 for client in clients)


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
async def test_bounded_gap_missing_source_blocks_exact_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One exact reply cannot hide a source omitted by bounded pagination."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    oracle.next_batch = "since"
    oracle.arm_gap_audit()
    oracle.expect("op:1", "$source")
    oracle.gap_audit_missing_sources.add("$source")
    oracle._gap_audit_completed = True
    oracle._ingest_event(
        _agent_reply_event(
            "$source",
            "$response",
            fuzz_live_matrix._ModelHandler.response_text_for(1),
        ),
    )

    async def sync_once(
        *,
        timeout_ms: int,
        allow_limited: bool = False,
        timeline_limit: int = 2000,
    ) -> None:
        assert (timeout_ms, allow_limited, timeline_limit) == (250, False, 2000)

    monkeypatch.setattr(oracle, "_sync_once", sync_once)
    try:
        with pytest.raises(AssertionError, match=r"bounded_gap_missing=.*op:1"):
            await oracle.wait_until_exact(deadline_seconds=0.01, settle_seconds=0)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_saturation_client_hydrates_every_limited_gap_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hidden duplicate remains visible to the saturation union audit."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    client.next_batch = "since"
    visible = {"event_id": "$visible"}
    hidden = {"event_id": "$hidden-duplicate"}

    async def sync(
        since: str | None,
        *,
        timeout_ms: int,
        timeline_limit: int = 2000,
    ) -> dict[str, Any]:
        assert (since, timeout_ms, timeline_limit) == ("since", 0, 2000)
        return {
            "next_batch": "next",
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": True,
                            "prev_batch": "page-1",
                            "events": [visible],
                        },
                    },
                },
            },
        }

    async def messages_before(
        from_token: str,
        *,
        to_token: str | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], str | None]:
        assert to_token == "since"  # noqa: S105 - opaque sync token
        assert limit == 1000
        if from_token == "page-1":  # noqa: S105 - opaque pagination token
            return [], "page-2"
        assert from_token == "page-2"  # noqa: S105 - opaque pagination token
        return [hidden], None

    monkeypatch.setattr(client, "sync", sync)
    monkeypatch.setattr(client, "messages_before", messages_before)
    try:
        await client.sync_incremental(timeout_ms=0, allow_limited=True)
    finally:
        await client.close()

    assert set(client.seen_events) == {"$visible", "$hidden-duplicate"}
    assert client.pagination_page_count == 2


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
async def test_exact_reply_oracle_rejects_unexpected_router_source() -> None:
    """Router-authored events cannot hide an otherwise unexpected agent reply."""
    client = LiveMatrixClient("http://matrix.invalid", "!room:example")
    oracle = ExactReplyOracle(client, "@agent:example")
    try:
        oracle._ingest_event(
            {
                "event_id": "$router-source",
                "sender": "@router:example",
                "type": "m.room.message",
                "content": {"body": "arbitrary source"},
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
                        "m.in_reply_to": {"event_id": "$router-source"},
                    },
                },
            },
        )
        with pytest.raises(AssertionError, match="unexpected"):
            oracle._assert_no_wrong_replies()
    finally:
        await client.close()
