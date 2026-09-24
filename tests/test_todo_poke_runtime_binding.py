"""Todo poke runtime coordinator tests."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.custom_tools.todo_poke import TodoPokeDeliveryUnavailableError
from mindroom.entity_resolution import mindroom_user_id
from mindroom.handled_turns import TurnRecord
from mindroom.hooks.sender import send_hook_message
from mindroom.ingress_validation import IngressValidator, IngressValidatorDeps
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.orchestration.todo_poke_runtime import TodoPokeRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.turn_policy import TurnPolicy
from tests.authorization_helpers import make_test_turn_policy_deps
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from mindroom.bot import AgentBot, TeamBot
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
    config: Config | None,
    bots: dict[str, AgentBot | TeamBot],
) -> TodoPokeRuntimeCoordinator:
    return TodoPokeRuntimeCoordinator(
        runtime_paths=runtime_paths,
        config_provider=lambda: config,
        bot_provider=bots.get,
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
    router_bot._hook_send_message = AsyncMock(return_value="$event")
    agent_bot = _bot()
    agent_bot._hook_send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"router": router_bot, "code": agent_bot},
    )

    with patch(
        "mindroom.orchestration.todo_poke_runtime.get_pending_schedule_thread_ids_for_room",
        new=AsyncMock(return_value=frozenset({"$scheduled"})),
    ) as schedule_query:
        pending = await coordinator._schedule_query("!room:localhost", ("code",))
        event_id = await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            "@alice:localhost",
        )

    assert pending == frozenset({"$scheduled"})
    schedule_query.assert_awaited_once_with(agent_bot.client, "!room:localhost")
    assert event_id == "$event"
    agent_bot._hook_send_message.assert_awaited_once_with(
        "!room:localhost",
        "@code Todo work is ready.",
        "$thread",
        "todo_poke",
        {ORIGINAL_SENDER_KEY: "@alice:localhost"},
        trigger_dispatch=True,
    )
    router_bot._hook_send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_refuses_requester_outside_access_policy_model(tmp_path: Path) -> None:
    """A bot account is neither a human nor an internal sender, so its poke would bypass the assignee's policy."""
    config = _restricted_config(tmp_path)
    client = _client("!room:localhost")
    agent_bot = _bot(client=client)
    agent_bot._hook_send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(runtime_paths_for(config), config, {"secret": agent_bot})

    event_id = await coordinator._send_poke(
        "secret",
        "!room:localhost",
        "@secret Todo work is ready.",
        "$thread",
        "@bridge:localhost",
    )

    assert event_id is None
    agent_bot._hook_send_message.assert_not_awaited()
    client.joined_rooms.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("requester", ["agent", "internal-user"])
async def test_send_pokes_internal_requesters(tmp_path: Path, requester: str) -> None:
    """Agent-authored work keeps poking, which keeps autonomous agent-to-agent todo loops alive."""
    config = _restricted_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    requester_ids = {
        "agent": entity_ids(config, runtime_paths)["code"].full_id,
        "internal-user": mindroom_user_id(config, runtime_paths),
    }
    requester_id = requester_ids[requester]
    assert requester_id is not None
    agent_bot = _bot(client=_client("!room:localhost"))
    agent_bot._hook_send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(runtime_paths, config, {"secret": agent_bot})

    event_id = await coordinator._send_poke(
        "secret",
        "!room:localhost",
        "@secret Todo work is ready.",
        "$thread",
        requester_id,
    )

    assert event_id == "$event"
    agent_bot._hook_send_message.assert_awaited_once_with(
        "!room:localhost",
        "@secret Todo work is ready.",
        "$thread",
        "todo_poke",
        {ORIGINAL_SENDER_KEY: requester_id},
        trigger_dispatch=True,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_poke_applies_assignee_access_policy_to_recorded_requester(tmp_path: Path) -> None:
    """The assignee's ingress must resolve the poke's requester to the human and enforce its policy."""
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

    async def hook_send_message(
        room_id: str,
        body: str,
        thread_id: str | None,
        source_hook: str,
        extra_content: dict[str, Any] | None,
        *,
        trigger_dispatch: bool = False,
    ) -> str | None:
        return await send_hook_message(
            MagicMock(),
            config,
            runtime_paths,
            room_id,
            body,
            thread_id,
            source_hook,
            extra_content,
            trigger_dispatch=trigger_dispatch,
            conversation_reader=make_conversation_reader_mock(),
        )

    agent_bot = _bot()
    agent_bot._hook_send_message = hook_send_message
    coordinator = _coordinator(runtime_paths, config, {"secret": agent_bot})
    with patch("mindroom.matrix.client_delivery.send_message_result", new=send_message_result):
        for requester_id in ("@alice:localhost", "@mallory:localhost", ids["code"].full_id):
            await coordinator._send_poke(
                "secret",
                "!room:localhost",
                "@secret Todo work is ready.",
                None,
                requester_id,
            )

    runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=None,
    )
    turn_store = MagicMock()
    turn_store.is_handled.return_value = False
    turn_store.record_turn = AsyncMock()
    validator = IngressValidator(
        IngressValidatorDeps(
            runtime=runtime,
            runtime_paths=runtime_paths,
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
        ),
    )
    room = nio.MatrixRoom("!room:localhost", secret_id)
    authorized_event, unauthorized_event, agent_event = (
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
        ids["code"].full_id,
    ]
    assert await validator.precheck_event(room, authorized_event) == "@alice:localhost"
    assert await validator.precheck_event(room, unauthorized_event) is None
    # An agent requester is never restricted by access, so the poke dispatches as the assignee's own turn.
    assert await validator.precheck_event(room, agent_event) == secret_id
    turn_store.record_turn.assert_awaited_once_with(TurnRecord.create([unauthorized_event.event_id]))


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
    schedule_query.assert_awaited_once_with(joined_client, "!room:localhost")


@pytest.mark.asyncio
async def test_send_skips_unjoined_todo_owner_without_using_other_candidate(tmp_path: Path) -> None:
    """Only the todo owner may transport its poke, and it must be joined."""
    unjoined_client = _client()
    joined_client = _client("!room:localhost")
    unjoined_bot = _bot(client=unjoined_client)
    unjoined_bot._hook_send_message = AsyncMock(return_value="$wrong")
    joined_bot = _bot(client=joined_client)
    joined_bot._hook_send_message = AsyncMock(return_value="$event")
    coordinator = _coordinator(
        test_runtime_paths(tmp_path),
        _config(tmp_path),
        {"code": unjoined_bot, "reviewer": joined_bot},
    )

    with pytest.raises(TodoPokeDeliveryUnavailableError):
        await coordinator._send_poke(
            "code",
            "!room:localhost",
            "@code Todo work is ready.",
            "$thread",
            "@alice:localhost",
        )

    unjoined_bot._hook_send_message.assert_not_awaited()
    joined_bot._hook_send_message.assert_not_awaited()


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
    schedule_query.assert_awaited_once_with(joined_client, "!room:localhost")


@pytest.mark.asyncio
async def test_send_rejects_stale_joined_room_cache(tmp_path: Path) -> None:
    """Cached room state must not authorize delivery after membership ended."""
    stale_client = _client(cached_room_ids=("!room:localhost",))
    stale_bot = _bot(client=stale_client)
    stale_bot._hook_send_message = AsyncMock(return_value="$wrong")
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
            "@alice:localhost",
        )

    stale_bot._hook_send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_maps_membership_probe_failure_to_delivery_unavailable(tmp_path: Path) -> None:
    """A failed membership probe must remain a no-attempt delivery outcome."""
    failing_client = _client()
    failing_client.joined_rooms = AsyncMock(side_effect=RuntimeError("membership unavailable"))
    failing_bot = _bot(client=failing_client)
    failing_bot._hook_send_message = AsyncMock(return_value="$wrong")
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
            "@alice:localhost",
        )

    failing_bot._hook_send_message.assert_not_awaited()


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
            "@alice:localhost",
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


@pytest.mark.asyncio
async def test_sync_wires_coordinator_adapters_into_worker(tmp_path: Path) -> None:
    """The composition seam must install the production idle, schedule, and sender adapters."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    worker = coordinator._worker
    assert worker is not None
    try:
        assert worker.deps.idle_check == coordinator._agent_is_idle
        assert worker.deps.schedule_query == coordinator._schedule_query
        assert worker.deps.sender == coordinator._send_poke
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_worker_lifecycle_survives_reload_and_stops(tmp_path: Path) -> None:
    """Sync starts one worker, reuses it on reload, and stops it promptly."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    first_worker = coordinator._worker
    first_task = coordinator._task

    assert first_worker is not None
    assert first_task is not None
    assert first_task.done() is False

    await coordinator.sync()

    assert coordinator._worker is first_worker
    assert coordinator._task is first_task

    await coordinator.stop()

    assert first_task.done() is True
    assert coordinator._worker is None
    assert coordinator._task is None


@pytest.mark.asyncio
async def test_worker_restarts_after_task_finishes(tmp_path: Path) -> None:
    """A finished worker task is replaced on the next sync."""
    coordinator = _coordinator(test_runtime_paths(tmp_path), _config(tmp_path), {})

    await coordinator.sync()
    first_worker = coordinator._worker
    first_task = coordinator._task
    assert first_worker is not None
    assert first_task is not None

    first_worker.stop()
    await first_task
    await coordinator.sync()

    assert coordinator._worker is not first_worker
    assert coordinator._task is not first_task
    await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by", ["zero-interval", "missing-config"])
async def test_sync_leaves_worker_stopped_when_disabled(tmp_path: Path, disabled_by: str) -> None:
    """A zero runtime interval or missing config leaves no worker or task running."""
    runtime_paths = test_runtime_paths(tmp_path)
    config: Config | None = _config(tmp_path)
    if disabled_by == "zero-interval":
        runtime_paths = replace(
            runtime_paths,
            process_env={"MINDROOM_TODO_POKE_INTERVAL_SECONDS": "0"},
        )
    else:
        config = None
    coordinator = _coordinator(runtime_paths, config, {})

    await coordinator.sync()

    assert coordinator._worker is None
    assert coordinator._task is None


def test_orchestrator_composes_live_coordinator_providers(tmp_path: Path) -> None:
    """Orchestrator wiring must expose live config and bot state to the coordinator."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    coordinator = orchestrator._todo_poke_runtime

    assert coordinator.runtime_paths is orchestrator.runtime_paths
    assert coordinator.config_provider() is None

    config = _config(tmp_path)
    orchestrator.config = config
    assert coordinator.config_provider() is config

    sentinel = _bot()
    orchestrator.agent_bots["code"] = sentinel
    assert coordinator.bot_provider("code") is sentinel
    assert coordinator.bot_provider("missing") is None
