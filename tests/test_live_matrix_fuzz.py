"""Tests for replayable real-server Matrix fuzz traces and their oracle."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.cancellation import SYNC_RESTART_CANCEL_MSG
from mindroom.coalescing import CoalescingDrainResult
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import AUTO_RESUME_MESSAGE, TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_checkpoint, save_sync_token
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import runtime as runtime_helpers
from mindroom.orchestration.runtime import _MatrixSyncStalledError, _SyncIteration, sync_forever_with_restart
from scripts.testing.fuzz_live_matrix import (
    ExactReplyOracle,
    LiveFuzzScenario,
    LiveMatrixClient,
    LiveOperation,
    LiveOperationKind,
    live_scenario_from_seed,
    restart_failure,
    saturation_scenario,
)
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


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


@pytest.mark.asyncio
async def test_restart_regression_scenario(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold-sync history must stay silent while fresh and resume events reply once."""
    seed = 20_260_729
    observations: list[tuple[int, int, str, str, str]] = []
    config = bind_runtime_paths(
        Config(
            agents={"restart_scenario": AgentConfig(display_name="RestartScenario", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )

    def make_bot() -> AgentBot:
        return install_runtime_cache_support(
            AgentBot(
                agent_user=AgentMatrixUser(
                    agent_name="restart_scenario",
                    password=TEST_PASSWORD,
                    display_name="RestartScenario",
                    user_id="@mindroom_restart_scenario:localhost",
                ),
                storage_path=tmp_path,
                config=config,
                runtime_paths=runtime_paths_for(config),
                rooms=["!room:localhost"],
            ),
        )

    old_bot = make_bot()
    save_sync_token(
        tmp_path,
        old_bot.agent_name,
        "s_certified_before_shutdown",
        cache_generation=old_bot.event_cache.cache_generation,
    )
    old_bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    old_bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_certified_before_shutdown")
    old_bot._response_runner._in_flight_response_count = 2
    old_bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)
    old_bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    initial_checkpoint_exists = load_sync_checkpoint(tmp_path, old_bot.agent_name) is not None
    active_responses_at_shutdown = old_bot.in_flight_response_count
    await old_bot.prepare_for_sync_shutdown()

    clear_sync_token(tmp_path, old_bot.agent_name)  # Keep the cold-sync fault explicit after retention fixes.
    replacement = make_bot()
    client = make_matrix_client_mock(user_id=replacement.agent_user.user_id)
    phase = "cold_initial_sync"
    event_coordinates = {
        "$old-text": (1, 0, "historical_text"),
        "$old-media": (2, 1, "historical_media"),
        "$fresh": (5, 2, "fresh_user"),
        "$auto-resume": (6, 3, "auto_resume"),
    }

    async def text_output(_room: nio.MatrixRoom, event: nio.RoomMessageText, **_kwargs: object) -> None:
        step, thread, category = event_coordinates[event.event_id]
        if category == "historical_text":
            observations.append((step, thread, category, phase, "processing"))
            return
        observations.extend((step, thread, category, phase, output) for output in ("foreground_read", "reply"))

    async def media_output(_room: nio.MatrixRoom, event: object, **_kwargs: object) -> None:
        step, thread, category = event_coordinates[cast("Any", event).event_id]
        observations.append((step, thread, category, phase, "transcription"))

    replacement._turn_controller.handle_text_event = AsyncMock(side_effect=text_output)
    replacement._turn_controller.handle_media_event = AsyncMock(side_effect=media_output)
    replacement._conversation_cache.cache_sync_timeline_for_certification = AsyncMock(
        return_value=SyncCacheWriteResult(complete=True),
    )
    replacement._run_sync_response_side_effects = AsyncMock()
    replacement.client = client
    with patch("mindroom.bot_runtime_view.time.time", return_value=100.0):
        replacement._runtime_view.mark_runtime_started()
    await replacement._prepare_matrix_sync_continuity()
    cold_sync_started = client.next_batch is None

    def text_event(event_id: str, timestamp: int, *, content: dict[str, object] | None = None) -> nio.RoomMessageText:
        return cast(
            "nio.RoomMessageText",
            nio.RoomMessageText.from_dict(
                {
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": timestamp,
                    "type": "m.room.message",
                    "content": content or {"msgtype": "m.text", "body": "synthetic"},
                },
            ),
        )

    old_text = text_event("$old-text", 10)
    old_media = MagicMock(spec=nio.RoomMessageAudio)
    old_media.event_id = "$old-media"
    old_media.sender = "@user:localhost"
    old_media.server_timestamp = 20
    old_media.source = {"event_id": "$old-media", "origin_server_ts": 20, "content": {"msgtype": "m.audio"}}
    fresh = text_event("$fresh", 100_001)
    auto_resume = text_event(
        "$auto-resume",
        100_002,
        content={
            "msgtype": "m.text",
            "body": AUTO_RESUME_MESSAGE,
            ORIGINAL_SENDER_KEY: "@user:localhost",
            SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        },
    )
    room = nio.MatrixRoom("!dormant:localhost", replacement.agent_user.user_id)

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_certified_after_restart"
    response.rooms = MagicMock(join={}, leave={})
    sync_attempts = 0
    stall_ready = asyncio.Event()

    async def sync_once() -> None:
        nonlocal phase, sync_attempts
        sync_attempts += 1
        phase = "cold_initial_sync" if sync_attempts == 1 else "watchdog_retry"
        await replacement._on_message(room, old_text)
        await replacement._on_media_message(room, old_media)
        if sync_attempts == 1:
            stall_ready.set()
            await asyncio.Event().wait()
        await replacement._on_message(room, fresh)
        await replacement._on_message(room, auto_resume)
        await replacement._on_sync_response(response)
        replacement.running = False

    remaining_stalls = 1

    async def watch(
        _bot: AgentBot,
        sync_task: asyncio.Task[object],
        watchdog_cancelled_sync: asyncio.Event,
    ) -> None:
        nonlocal remaining_stalls
        if remaining_stalls:
            remaining_stalls -= 1
            await stall_ready.wait()
            watchdog_cancelled_sync.set()
            sync_task.cancel(msg=SYNC_RESTART_CANCEL_MSG)
            await asyncio.gather(sync_task, return_exceptions=True)
            raise _MatrixSyncStalledError
        await sync_task

    replacement.sync_forever = AsyncMock(side_effect=sync_once)
    monkeypatch.setattr(_SyncIteration, "_watch", staticmethod(watch))
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)
    replacement.running = True
    await sync_forever_with_restart(replacement, max_retries=2)
    watchdog_retries = sync_attempts - 1
    first_sync_completions = int(replacement._first_sync_done)
    final_checkpoint_exists = load_sync_checkpoint(tmp_path, replacement.agent_name) is not None
    await client.close()

    def count(category: str, output: str) -> int:
        return sum(item[2] == category and item[4] == output for item in observations)

    failures = [
        restart_failure(
            "historical_output_suppressed",
            seed=seed,
            event_category=category,
            phase=output_phase,
            observed=f"output:{output}",
            step=step,
            thread=thread,
        )
        for step, thread, category, output_phase, output in observations
        if category in {"historical_text", "historical_media"}
    ]
    checks = (
        ("fresh_event_exactly_once", count("fresh_user", "reply"), 1, "fresh_user", phase),
        ("foreground_read_exactly_once", count("fresh_user", "foreground_read"), 1, "fresh_user", phase),
        ("auto_resume_exactly_once", count("auto_resume", "reply"), 1, "auto_resume", phase),
        ("certified_checkpoint_precondition", initial_checkpoint_exists, True, "scenario", "shutdown"),
        ("two_active_responses_precondition", active_responses_at_shutdown, 2, "scenario", "shutdown"),
        ("tokenless_cold_sync_precondition", cold_sync_started, True, "scenario", "cold_initial_sync"),
        ("watchdog_retry_bounded", watchdog_retries, 1, "scenario", phase),
        ("first_sync_exactly_once", first_sync_completions, 1, "scenario", "certification"),
        ("certified_checkpoint_after_sync", final_checkpoint_exists, True, "scenario", "certification"),
    )
    failures.extend(
        restart_failure(
            invariant,
            seed=seed,
            event_category=category,
            phase=check_phase,
            observed=observed,
        )
        for invariant, observed, expected, category, check_phase in checks
        if observed != expected
    )
    assert not failures, "restart regression invariant failures:\n" + "\n".join(failures)


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
