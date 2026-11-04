"""Todo poke runtime coordinator tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.custom_tools.todo_poke import (
    TodoPokeDeliveryUnavailableError,
    TodoPokeDeps,
    TodoPokePolicy,
    TodoPokeRequesterKind,
    TodoPokeWorker,
    scan_todo_pokes,
)
from mindroom.custom_tools.todo_state import state_root as todo_state_root
from mindroom.entity_resolution import MissingManagedEntityAccountError, mindroom_user_id
from mindroom.hooks.sender import build_hook_message_sender
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.turn_policy import TurnPolicy
from mindroom.turn_record import TurnRecord
from tests.authorization_helpers import make_test_turn_policy_deps
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code")},
            teams={
                "dev": TeamConfig(
                    display_name="Dev",
                    role="Develop",
                    agents=["code"],
                ),
            },
        ),
        runtime_paths=runtime_paths,
    )


def _restricted_config(tmp_path: Path) -> Config:
    """Return a config where only `@alice` may address the `secret` agent."""
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"]),
                "secret": AgentConfig(
                    display_name="Secret",
                    rooms=["!room:localhost"],
                    access=ResponderAccessConfig(users=["@alice:localhost"]),
                ),
            },
            bot_accounts=["@bridge:localhost"],
            mindroom_user=MindRoomUserConfig(),
        ),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def _coordinator(
    runtime_paths: RuntimePaths,
    config: Config,
    bots: dict[str, AgentBot],
) -> TodoPokeRuntimeCoordinator:
    return TodoPokeRuntimeCoordinator(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bot_provider=bots.get,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )


def _client(
    *joined_room_ids: str,
    cached_room_ids: tuple[str, ...] | None = None,
) -> MagicMock:
    client = MagicMock()
    cached_ids = joined_room_ids if cached_room_ids is None else cached_room_ids
    client.rooms = {room_id: MagicMock() for room_id in cached_ids}
    client.joined_rooms = AsyncMock(
        return_value=nio.JoinedRoomsResponse(rooms=list(joined_room_ids)),
    )
    return client


def _bot(**overrides: object) -> MagicMock:
    bot = MagicMock()
    bot.running = overrides.get("running", True)
    if "client" in overrides:
        bot.client = overrides["client"]
    else:
        bot.client = _client("!room:localhost")
    bot.in_flight_response_count = overrides.get("in_flight_response_count", 0)
    return bot


def test_idle_check_includes_direct_and_running_team_bots(tmp_path: Path) -> None:
    """Direct activity or activity in any running member team makes the agent busy."""
    direct_bot = _bot()
    team_bot = _bot()
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": direct_bot, "dev": team_bot},
    )

    assert coordinator._agent_is_idle("code") is True

    direct_bot.in_flight_response_count = 1
    assert coordinator._agent_is_idle("code") is False

    direct_bot.in_flight_response_count = 0
    team_bot.in_flight_response_count = 1
    assert coordinator._agent_is_idle("code") is False

    team_bot.running = False
    assert coordinator._agent_is_idle("code") is True
    assert coordinator._agent_is_idle("removed") is False


@pytest.mark.asyncio
async def test_assigned_agent_query_and_send_wiring(tmp_path: Path) -> None:
    """Todo poke I/O uses the assigned agent that owns membership in the target room."""
    router_bot = _bot()
    router_bot.hook_context.send_message = AsyncMock(return_value="$event")
    agent_bot = _bot()
    agent_bot.hook_context.send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"router": router_bot, "code": agent_bot},
    )

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
            new=AsyncMock(return_value=frozenset({"$scheduled"})),
        ) as schedule_query,
        patch(
            "mindroom.orchestration.todo_poke_runtime.mindroom_user_id",
            return_value="@mindroom_user:localhost",
        ),
    ):
        pending = await coordinator._schedule_query("!room:localhost", ("code",))
        event_id = await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            None,
        )

    assert pending == frozenset({"$scheduled"})
    schedule_query.assert_awaited_once_with(agent_bot.client, "!room:localhost", coordinator.runtime_paths)
    assert event_id == "$event"
    agent_bot.hook_context.send_message.assert_awaited_once_with(
        "!room:localhost",
        "@code Todo work is ready.",
        "$thread",
        "todo_poke",
        {ORIGINAL_SENDER_KEY: "@mindroom_user:localhost"},
        trigger_dispatch=True,
    )
    router_bot.hook_context.send_message.assert_not_awaited()

    agent_bot.hook_context.send_message.reset_mock()
    await coordinator._send_poke("code", "!room:localhost", "@code Todo work is ready.", "$thread", "@alice:localhost")

    agent_bot.hook_context.send_message.assert_awaited_once_with(
        "!room:localhost",
        "@code Todo work is ready.",
        "$thread",
        "todo_poke",
        {ORIGINAL_SENDER_KEY: "@alice:localhost"},
        trigger_dispatch=True,
    )


@pytest.mark.usefixtures("enforce_turn_authorization")
def test_requester_kind_follows_current_config(tmp_path: Path) -> None:
    """Allowed humans keep their identity, admitted MindRoom accounts run as the assignee, and others are refused."""
    config = _restricted_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    coordinator = _coordinator(runtime_paths, config, {})

    def kind(requester_id: str | None, agent_name: str = "secret") -> TodoPokeRequesterKind:
        assert requester_id is not None
        return coordinator._requester_kind(requester_id, agent_name, "!room:localhost")

    assert kind("@alice:localhost") is TodoPokeRequesterKind.HUMAN
    # A human the assignee no longer replies to would only have the poke ignored at ingress.
    assert kind("@mallory:localhost") is TodoPokeRequesterKind.REFUSED
    assert kind("@alice:localhost", "removed") is TodoPokeRequesterKind.REFUSED
    assert kind(ids["secret"].full_id) is TodoPokeRequesterKind.INTERNAL
    # Work another agent recorded with no person behind it pokes only an assignee whose access admits that agent.
    assert kind(ids["code"].full_id) is TodoPokeRequesterKind.REFUSED
    assert kind(ids["router"].full_id) is TodoPokeRequesterKind.REFUSED
    assert kind(mindroom_user_id(config, runtime_paths)) is TodoPokeRequesterKind.REFUSED
    assert kind("@bridge:localhost") is TodoPokeRequesterKind.REFUSED
    config.agents["secret"].access = ResponderAccessConfig(users=["@alice:localhost", ids["code"].full_id])
    assert kind(ids["code"].full_id) is TodoPokeRequesterKind.INTERNAL

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.current_internal_sender_ids",
            side_effect=MissingManagedEntityAccountError("not prepared"),
        ),
        pytest.raises(TodoPokeDeliveryUnavailableError),
    ):
        kind("@alice:localhost")


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_scan_waits_for_pending_membership_without_pruning_human_record(tmp_path: Path) -> None:
    """An unresolved membership grant is no refusal, so the human's dedup record survives until it resolves."""
    config = bind_runtime_paths(
        Config(
            agents={
                "shared": AgentConfig(
                    display_name="Shared",
                    rooms=["!room:localhost"],
                    access=ResponderAccessConfig(current_room_members=True),
                ),
            },
        ),
        runtime_paths=test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    entity_ids(config, runtime_paths)
    # A fresh index has not yet loaded the room's members, as after a restart or a membership invalidation.
    coordinator = _coordinator(runtime_paths, config, {})
    todo_root = todo_state_root(runtime_paths)
    thread_path = todo_root / "threads" / "scope" / "todos.json"
    thread_path.parent.mkdir(parents=True)
    thread_path.write_text(
        json.dumps(
            {
                "room_id": "!room:localhost",
                "thread_id": "$thread",
                "items": [
                    {
                        "id": "carol",
                        "title": "Carol work",
                        "status": "open",
                        "priority": "medium",
                        "depends_on": [],
                        "assigned_agent": "shared",
                        "requester_id": "@carol:localhost",
                        "updated_at": "2026-07-18T11:50:00+00:00",
                    },
                ],
            },
        ),
        encoding="utf-8",
    )
    poke_state_path = todo_root / "poke_state.json"
    seeded_state = {
        "scopes": {
            '["shared","!room:localhost","$thread","@carol:localhost"]': {
                "last_poked_at": 0.0,
                "last_fingerprint": "exhausted",
                "unchanged_repoke_count": 3,
            },
        },
    }
    poke_state_path.write_text(json.dumps(seeded_state), encoding="utf-8")
    sender = AsyncMock(return_value="$event")
    deps = TodoPokeDeps(
        state_root=todo_root,
        schedule_query=AsyncMock(return_value=frozenset()),
        idle_check=lambda _agent_name: True,
        sender=sender,
        requester_kind=coordinator._requester_kind,
        clock=lambda: datetime.now(UTC),
    )

    assert await scan_todo_pokes(TodoPokePolicy(quiet_seconds=0), deps) == 0

    sender.assert_not_awaited()
    assert json.loads(poke_state_path.read_text(encoding="utf-8")) == seeded_state
    with pytest.raises(TodoPokeDeliveryUnavailableError) as raised:
        coordinator._requester_kind("@carol:localhost", "shared", "!room:localhost")
    assert isinstance(raised.value.__cause__, ReplyMembershipPendingError)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_poke_applies_assignee_access_policy_to_human_requester(tmp_path: Path) -> None:
    """The assignee's ingress resolves a human poke to that human, while an internal poke stays the assignee's turn."""
    config = _restricted_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    ids = entity_ids(config, runtime_paths)
    secret_id = ids["secret"].full_id
    sent_contents: list[dict[str, Any]] = []

    async def send_message_result(
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        sent_contents.append(content)
        return DeliveredMatrixEvent(event_id=f"$poke-{len(sent_contents)}", content_sent=content)

    agent_bot = _bot()
    agent_bot.hook_context.send_message = build_hook_message_sender(
        MagicMock(),
        config,
        runtime_paths,
        conversation_reader=make_conversation_reader_mock(),
    )
    coordinator = _coordinator(runtime_paths, config, {"secret": agent_bot})
    with patch("mindroom.matrix.client_delivery.send_message_result", new=send_message_result):
        for requester_id in ("@alice:localhost", "@mallory:localhost", None):
            await coordinator._send_poke("secret", "!room:localhost", "@secret Todo work is ready.", None, requester_id)

    config.defaults.enable_streaming = False
    runtime = BotRuntimeState(orchestrator=ready_runtime(config, runtime_paths))
    turn_store = MagicMock()
    turn_store.is_handled = AsyncMock(return_value=False)
    turn_store.record_turn = AsyncMock()
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
            agent_name="secret",
            matrix_id=ids["secret"],
            turn_store=turn_store,
            turn_policy=TurnPolicy(
                make_test_turn_policy_deps(
                    runtime=runtime,
                    logger=get_logger(__name__),
                    runtime_paths=runtime_paths,
                    agent_name="secret",
                    matrix_id=ids["secret"],
                    agent_reply_memberships=runtime.agent_reply_memberships,
                ),
            ),
            settle_ignored_sources=AsyncMock(),
        ),
    )
    room = nio.MatrixRoom("!room:localhost", secret_id)
    authorized_event, unauthorized_event, internal_event = (
        nio.RoomMessageText.from_dict(
            {
                "event_id": f"$poke-{index}",
                "sender": secret_id,
                "origin_server_ts": 1234567890,
                "content": content,
            },
        )
        for index, content in enumerate(sent_contents, start=1)
    )

    assert [content[ORIGINAL_SENDER_KEY] for content in sent_contents] == [
        "@alice:localhost",
        "@mallory:localhost",
        mindroom_user_id(config, runtime_paths),
    ]
    authorized_facts = validator.classify(authorized_event, room_id=room.room_id)
    assert authorized_facts.lane_requester_id == "@alice:localhost"
    assert await validator.precheck_event(room, authorized_event, authorized_facts)
    # A human who may not address the assignee gets no turn from the poke.
    assert not await validator.precheck_event(
        room,
        unauthorized_event,
        validator.classify(unauthorized_event, room_id=room.room_id),
    )
    internal_facts = validator.classify(internal_event, room_id=room.room_id)
    assert internal_facts.lane_requester_id == secret_id
    assert await validator.precheck_event(room, internal_event, internal_facts)
    turn_store.record_turn.assert_awaited_once_with(
        TurnRecord.create([unauthorized_event.event_id]),
        room_id=room.room_id,
    )


@pytest.mark.asyncio
async def test_schedule_query_uses_joined_candidate_for_shared_room(tmp_path: Path) -> None:
    """A same-room assignee without membership must not hide a later joined assignee."""
    unjoined_client = _client()
    joined_client = _client("!room:localhost")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {
            "code": _bot(client=unjoined_client),
            "reviewer": _bot(client=joined_client),
        },
    )

    with patch(
        "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
        new=AsyncMock(return_value=frozenset({"$scheduled"})),
    ) as schedule_query:
        pending = await coordinator._schedule_query(
            "!room:localhost",
            ("code", "reviewer"),
        )

    assert pending == frozenset({"$scheduled"})
    schedule_query.assert_awaited_once_with(joined_client, "!room:localhost", coordinator.runtime_paths)


@pytest.mark.asyncio
async def test_send_skips_unjoined_todo_owner_without_using_other_candidate(tmp_path: Path) -> None:
    """Only the todo owner may transport its poke, and it must be joined."""
    unjoined_client = _client()
    joined_client = _client("!room:localhost")
    unjoined_bot = _bot(client=unjoined_client)
    unjoined_bot.hook_context.send_message = AsyncMock(return_value="$wrong")
    joined_bot = _bot(client=joined_client)
    joined_bot.hook_context.send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": unjoined_bot, "reviewer": joined_bot},
    )

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.mindroom_user_id",
            return_value="@mindroom_user:localhost",
        ),
        pytest.raises(TodoPokeDeliveryUnavailableError),
    ):
        await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            None,
        )

    unjoined_bot.hook_context.send_message.assert_not_awaited()
    joined_bot.hook_context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_query_rejects_stale_joined_room_cache(tmp_path: Path) -> None:
    """Cached room state must not authorize a schedule read after membership ended."""
    stale_client = _client(cached_room_ids=("!room:localhost",))
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": _bot(client=stale_client)},
    )

    with patch(
        "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
        new=AsyncMock(return_value=frozenset({"$scheduled"})),
    ) as schedule_query:
        pending = await coordinator._schedule_query("!room:localhost", ("code",))

    assert pending is None
    schedule_query.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_query_continues_after_candidate_membership_probe_failure(tmp_path: Path) -> None:
    """One failed membership probe must not hide a later joined candidate."""
    failing_client = _client()
    failing_client.joined_rooms = AsyncMock(side_effect=RuntimeError("membership unavailable"))
    joined_client = _client("!room:localhost")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {
            "code": _bot(client=failing_client),
            "reviewer": _bot(client=joined_client),
        },
    )

    with patch(
        "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
        new=AsyncMock(return_value=frozenset({"$scheduled"})),
    ) as schedule_query:
        pending = await coordinator._schedule_query(
            "!room:localhost",
            ("code", "reviewer"),
        )

    assert pending == frozenset({"$scheduled"})
    schedule_query.assert_awaited_once_with(joined_client, "!room:localhost", coordinator.runtime_paths)


@pytest.mark.asyncio
async def test_send_rejects_stale_joined_room_cache(tmp_path: Path) -> None:
    """Cached room state must not authorize delivery after membership ended."""
    stale_client = _client(cached_room_ids=("!room:localhost",))
    stale_bot = _bot(client=stale_client)
    stale_bot.hook_context.send_message = AsyncMock(return_value="$wrong")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": stale_bot},
    )

    with pytest.raises(TodoPokeDeliveryUnavailableError):
        await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            None,
        )

    stale_bot.hook_context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_maps_membership_probe_failure_to_delivery_unavailable(tmp_path: Path) -> None:
    """A failed membership probe must remain a no-attempt delivery outcome."""
    failing_client = _client()
    failing_client.joined_rooms = AsyncMock(side_effect=RuntimeError("membership unavailable"))
    failing_bot = _bot(client=failing_client)
    failing_bot.hook_context.send_message = AsyncMock(return_value="$wrong")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": failing_bot},
    )

    with pytest.raises(TodoPokeDeliveryUnavailableError):
        await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            None,
        )

    failing_bot.hook_context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapters_skip_when_runtime_is_unavailable(tmp_path: Path) -> None:
    """Unavailable idle, schedule, and sender adapters conservatively skip the tick."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    assert coordinator._agent_is_idle("code") is False
    assert await coordinator._schedule_query("!room:localhost", ("code",)) is None
    with pytest.raises(TodoPokeDeliveryUnavailableError):
        await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            None,
        )


@pytest.mark.asyncio
async def test_schedule_adapter_preserves_read_errors(tmp_path: Path) -> None:
    """Read errors reach the scanner's tested fail-open boundary unchanged."""
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": _bot()},
    )

    with (
        patch(
            "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
            new=AsyncMock(side_effect=RuntimeError("state unavailable")),
        ),
        pytest.raises(RuntimeError, match="state unavailable"),
    ):
        await coordinator._schedule_query("!room:localhost", ("code",))


@pytest.fixture
def started_workers() -> Iterator[list[TodoPokeWorker]]:
    """Record each worker the coordinator runs, idling until it is asked to stop."""
    workers: list[TodoPokeWorker] = []

    async def run(worker: TodoPokeWorker) -> None:
        workers.append(worker)
        await worker._stop_event.wait()

    with patch.object(TodoPokeWorker, "run", run):
        yield workers


async def _until_started(workers: list[TodoPokeWorker], count: int = 1) -> None:
    for _ in range(500):
        if len(workers) >= count:
            return
        await asyncio.sleep(0.01)
    msg = "worker did not start"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_sync_wires_coordinator_adapters_into_worker(
    tmp_path: Path,
    started_workers: list[TodoPokeWorker],
) -> None:
    """The composition seam must install the production idle, schedule, and sender adapters."""
    config = _config(tmp_path)
    coordinator = _coordinator(test_runtime_paths(tmp_path), config, {})

    await coordinator.sync(config)
    try:
        await _until_started(started_workers)
        worker = started_workers[0]
        assert worker.deps.idle_check == coordinator._agent_is_idle
        assert worker.deps.schedule_query == coordinator._schedule_query
        assert worker.deps.sender == coordinator._send_poke
        assert worker.deps.requester_kind == coordinator._requester_kind
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_worker_lifecycle_survives_reload_and_stops(
    tmp_path: Path,
    started_workers: list[TodoPokeWorker],
) -> None:
    """Sync starts one worker, keeps it on reload, and stops it."""
    config = _config(tmp_path)
    coordinator = _coordinator(test_runtime_paths(tmp_path), config, {})

    await coordinator.sync(config)
    await _until_started(started_workers)
    await coordinator.sync(config)
    await coordinator.stop()

    assert len(started_workers) == 1
    assert started_workers[0]._stop_event.is_set()
    assert not coordinator._loop.running


@pytest.mark.asyncio
async def test_stop_lets_the_scan_in_progress_finish(tmp_path: Path) -> None:
    """Stopping asks the worker to stop and waits for its current scan, so a poke it sends is recorded."""
    config = _config(tmp_path)
    coordinator = _coordinator(test_runtime_paths(tmp_path), config, {})
    workers: list[TodoPokeWorker] = []
    release_scan = asyncio.Event()
    scan_completed = False

    async def run(worker: TodoPokeWorker) -> None:
        nonlocal scan_completed
        workers.append(worker)
        await release_scan.wait()
        scan_completed = True

    with patch.object(TodoPokeWorker, "run", run):
        await coordinator.sync(config)
        await _until_started(workers)
        stopping = asyncio.create_task(coordinator.stop())
        await asyncio.wait_for(workers[0]._stop_event.wait(), timeout=5)
        assert not stopping.done()

        release_scan.set()
        await asyncio.wait_for(stopping, timeout=5)

    assert scan_completed


@pytest.mark.asyncio
async def test_sync_leaves_worker_stopped_when_disabled(
    tmp_path: Path,
    started_workers: list[TodoPokeWorker],
) -> None:
    """A zero runtime interval stops a running worker and starts none."""
    config = _config(tmp_path)
    coordinator = _coordinator(test_runtime_paths(tmp_path), config, {})
    await coordinator.sync(config)
    await _until_started(started_workers)

    coordinator.runtime_paths = replace(
        coordinator.runtime_paths,
        process_env={"MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0"},
    )
    await coordinator.sync(config)

    assert started_workers[0]._stop_event.is_set()
    assert not coordinator._loop.running


def test_orchestrator_composes_live_coordinator_providers(tmp_path: Path) -> None:
    """Orchestrator wiring must expose live config and bot state to the coordinator."""
    config = _config(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=config)
    coordinator = orchestrator._todo_poke_runtime

    assert coordinator.runtime_paths is orchestrator.runtime_paths
    assert coordinator.config_provider() is config

    published = _config(tmp_path)
    orchestrator.config = published
    assert coordinator.config_provider() is published

    sentinel = _bot()
    orchestrator.fleet.add("code", sentinel)
    assert coordinator.bot_provider("code") is sentinel
    assert coordinator.bot_provider("missing") is None
    assert coordinator.agent_reply_memberships is orchestrator.agent_reply_memberships
