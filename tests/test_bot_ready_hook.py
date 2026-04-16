"""Tests for the bot:ready lifecycle hook event."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookRegistry,
    hook,
)
from mindroom.hooks.types import BUILTIN_EVENT_NAMES, DEFAULT_EVENT_TIMEOUT_MS, RESERVED_EVENT_NAMESPACES
from mindroom.matrix.cache import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_runtime_cache_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code") -> AgentBot:
    config = _config(tmp_path)
    return install_runtime_cache_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name=agent_name,
                password=TEST_PASSWORD,
                display_name=agent_name.title(),
                user_id=f"@mindroom_{agent_name}:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
        ),
    )


def _bind_shared_runtime_support(
    orchestrator: MultiAgentOrchestrator,
    bots_by_name: dict[str, AgentBot],
) -> None:
    orchestrator.agent_bots = dict(bots_by_name)
    for bot in bots_by_name.values():
        orchestrator._bind_runtime_support_services(bot)
        bot.orchestrator = orchestrator


def _thread_root_event(
    event_id: str,
    *,
    body: str,
    origin_server_ts: int,
    room_id: str = "!room:localhost",
) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": room_id,
            "type": "m.room.message",
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


def test_bot_ready_is_a_builtin_event() -> None:
    """EVENT_BOT_READY should be registered as a built-in event."""
    assert EVENT_BOT_READY == "bot:ready"
    assert EVENT_BOT_READY in BUILTIN_EVENT_NAMES


def test_bot_ready_has_default_timeout() -> None:
    """bot:ready should have a default timeout of 5000ms."""
    assert DEFAULT_EVENT_TIMEOUT_MS[EVENT_BOT_READY] == 5000


def test_bot_namespace_is_reserved() -> None:
    """The 'bot' namespace should be reserved to prevent custom event collisions."""
    assert "bot" in RESERVED_EVENT_NAMESPACES


@pytest.mark.asyncio
async def test_bot_ready_fires_on_first_sync_response(tmp_path: Path) -> None:
    """bot:ready should fire when the first sync response is received."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_events: list[str] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        fired_events.append(ctx.event_name)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

    assert fired_events == ["bot:ready"]


@pytest.mark.asyncio
async def test_installed_runtime_cache_support_runs_fire_and_forget_sync_cache_writes(tmp_path: Path) -> None:
    """The shared test runtime helper must preserve the coordinator's synchronous queue contract."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Thread reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
            },
            "event_id": "$thread_msg:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )
    sync_response = MagicMock()
    sync_response.__class__ = nio.SyncResponse
    sync_response.rooms = MagicMock(
        join={
            "!room:localhost": MagicMock(timeline=MagicMock(events=[message_event])),
        },
    )

    bot._conversation_cache.cache_sync_timeline(sync_response)
    await bot.event_cache_write_coordinator.wait_for_room_idle("!room:localhost")

    bot.event_cache.store_events_batch.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_ready_fires_only_once(tmp_path: Path) -> None:
    """bot:ready should fire only on the first sync, not on subsequent syncs."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_count = 0

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired_count
        fired_count += 1

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())
        await bot._on_sync_response(MagicMock())
        await bot._on_sync_response(MagicMock())

    assert fired_count == 1


@pytest.mark.asyncio
async def test_bot_ready_fires_after_agent_started(tmp_path: Path) -> None:
    """bot:ready must fire after agent:started since it depends on sync being established."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    event_order: list[str] = []

    @hook(EVENT_AGENT_STARTED)
    async def on_started(_ctx: AgentLifecycleContext) -> None:
        event_order.append("agent:started")

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        event_order.append("bot:ready")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_started, on_ready])])

    # agent:started fires during start() setup
    await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    # bot:ready fires on first sync
    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

    assert event_order == ["agent:started", "bot:ready"]


@pytest.mark.asyncio
async def test_bot_ready_hook_can_send_messages(tmp_path: Path) -> None:
    """Hooks on bot:ready should be able to send messages through the bound sender."""
    bot = _agent_bot(tmp_path, agent_name="router")
    bot.client = AsyncMock()
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": bot}
    bot.orchestrator = orchestrator

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object]) -> object:
        captured_content.update(content)
        return delivered_matrix_event("$hook-event", content)

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        await ctx.send_message("!room:localhost", "I'm ready!")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.hooks.sender.send_message_result", side_effect=mock_send),
    ):
        await bot._on_sync_response(MagicMock())

    assert captured_content["com.mindroom.source_kind"] == "hook"
    assert captured_content["com.mindroom.hook_source"] == "test-plugin:bot:ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", [EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED])
async def test_lifecycle_hooks_prefer_bot_room_state_helpers_before_router_fallback(
    tmp_path: Path,
    event_name: str,
) -> None:
    """Lifecycle hooks should query room state with the current bot before falling back to the router."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Agent Lobby"})
    bot.client.room_put_state.return_value = object()
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Router Lobby"})
    router_bot.client.room_put_state.return_value = object()
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    results: list[tuple[dict[str, object] | None, bool]] = []

    @hook(event_name)
    async def on_lifecycle(ctx: AgentLifecycleContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$thread",
            {"tags": {"queued": True}},
        )
        results.append((query_result, put_result))

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_lifecycle])])

    await bot._emit_agent_lifecycle_event(event_name)

    assert results == [({"name": "Agent Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )
    router_bot.client.room_get_state_event.assert_not_awaited()
    router_bot.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", [EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED])
async def test_lifecycle_hooks_fallback_to_router_room_state_helpers_when_bot_cannot_access_room(
    tmp_path: Path,
    event_name: str,
) -> None:
    """Lifecycle hooks should fall back to the router when the current bot cannot access room state."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
    bot.client.room_put_state.return_value = nio.RoomPutStateError(message="forbidden")
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Router Lobby"})
    router_bot.client.room_put_state.return_value = object()
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    results: list[tuple[dict[str, object] | None, bool]] = []

    @hook(event_name)
    async def on_lifecycle(ctx: AgentLifecycleContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$thread",
            {"tags": {"queued": True}},
        )
        results.append((query_result, put_result))

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_lifecycle])])

    await bot._emit_agent_lifecycle_event(event_name)

    assert results == [({"name": "Router Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )
    router_bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    router_bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )


@pytest.mark.asyncio
async def test_bot_ready_does_not_fire_during_sync_shutdown(tmp_path: Path) -> None:
    """bot:ready must not fire if sync is shutting down."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired = False

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired
        fired = True

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])
    bot._sync_shutting_down = True

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

    assert not fired


@pytest.mark.asyncio
async def test_bot_ready_fires_after_shutdown_clears(tmp_path: Path) -> None:
    """bot:ready must fire after shutdown suppresses and then clears (restart recovery)."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_count = 0

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired_count
        fired_count += 1

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        # First sync arrives during shutdown — bot:ready suppressed
        bot._sync_shutting_down = True
        await bot._on_sync_response(MagicMock())
        assert fired_count == 0

        # Shutdown clears (restart)
        bot.mark_sync_loop_started()

        # Next sync — bot:ready must fire now
        await bot._on_sync_response(MagicMock())
        assert fired_count == 1

        # Subsequent syncs must not re-fire
        await bot._on_sync_response(MagicMock())
        assert fired_count == 1


@pytest.mark.asyncio
async def test_bot_ready_context_has_correct_entity_info(tmp_path: Path) -> None:
    """bot:ready context should carry the agent's name, type, and rooms."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

    assert len(captured_ctx) == 1
    ctx = captured_ctx[0]
    assert ctx.entity_name == "code"
    assert ctx.matrix_user_id == "@mindroom_code:localhost"
    assert "!room:localhost" in ctx.rooms
    assert ctx.joined_room_ids == ("!room:localhost",)


@pytest.mark.asyncio
async def test_lifecycle_context_preserves_configured_rooms_and_exposes_joined_room_ids(tmp_path: Path) -> None:
    """Lifecycle hooks should keep configured rooms separate from resolved Matrix room IDs."""
    bot = _agent_bot(tmp_path)
    bot.config.agents["code"].rooms = ["lobby", "!room:localhost"]
    bot.rooms = ["!room:localhost"]
    bot.client = AsyncMock()

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_AGENT_STARTED)
    async def on_started(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_started])])

    await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("lobby", "!room:localhost")
    assert captured_ctx[0].joined_room_ids == ("!room:localhost",)


@pytest.mark.asyncio
async def test_bot_ready_context_includes_joined_rooms_from_first_sync(tmp_path: Path) -> None:
    """bot:ready should expose rooms learned from the first sync response."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    bot.client.rooms = {"!joined:localhost": MagicMock()}

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("!room:localhost",)
    assert captured_ctx[0].joined_room_ids == ("!room:localhost", "!joined:localhost")


@pytest.mark.asyncio
async def test_bot_ready_starts_background_startup_thread_prewarm(tmp_path: Path) -> None:
    """bot:ready should prewarm recent thread snapshots in the background after first sync."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot._conversation_cache.logger = MagicMock()
    bot._conversation_cache._refresh_dispatch_thread_snapshot_for_startup_prewarm = AsyncMock(
        return_value=thread_history_result([], is_full_history=False),
    )

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1),
        _thread_root_event("$thread-b:localhost", body="Thread B", origin_server_ts=2),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, "next-token")),
        ) as mock_get_room_threads_page,
        patch.object(
            bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(side_effect=AssertionError("startup prewarm should bypass the live dispatch entrypoint")),
        ) as mock_get_dispatch_thread_snapshot,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        limit=20,
    )
    assert [
        call.args
        for call in bot._conversation_cache._refresh_dispatch_thread_snapshot_for_startup_prewarm.await_args_list
    ] == [
        ("!room:localhost", "$thread-a:localhost"),
        ("!room:localhost", "$thread-b:localhost"),
    ]
    mock_get_dispatch_thread_snapshot.assert_not_awaited()
    bot._conversation_cache.logger.info.assert_any_call(
        "startup_thread_prewarm_complete",
        room_id="!room:localhost",
        threads_warmed=2,
        threads_failed=0,
        elapsed_ms=ANY,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_joined_rooms_failure_is_fail_open(tmp_path: Path) -> None:
    """Startup thread prewarm should log and stop cleanly when joined room lookup fails."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.side_effect = RuntimeError("boom")
    bot._conversation_cache.logger = MagicMock()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.background_tasks.logger.exception") as mock_background_logger_exception,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_joined_rooms_failed",
        error="boom",
    )
    mock_background_logger_exception.assert_not_called()


@pytest.mark.asyncio
async def test_bot_ready_can_disable_startup_thread_prewarm(tmp_path: Path) -> None:
    """Per-bot config should allow startup thread prewarm to be disabled."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    rooms=["!room:localhost"],
                    startup_thread_prewarm=False,
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    bot = install_runtime_cache_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name="code",
                password=TEST_PASSWORD,
                display_name="Code",
                user_id="@mindroom_code:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
        ),
    )
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(),
        ) as mock_get_room_threads_page,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    mock_get_room_threads_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_only_ad_hoc_room_still_prewarms_when_router_exists(tmp_path: Path) -> None:
    """A non-router bot should prewarm its joined ad hoc room even when a router exists elsewhere."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!adhoc:localhost"])
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot})

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1, room_id="!adhoc:localhost"),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ) as mock_get_room_threads_page,
        patch.object(
            router_bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
        patch.object(
            agent_bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
    ):
        await router_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)
        await agent_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        agent_bot.client,
        "!adhoc:localhost",
        limit=20,
    )


@pytest.mark.asyncio
async def test_first_syncing_bot_wins_shared_room_startup_prewarm_claim(tmp_path: Path) -> None:
    """When multiple bots share a room, the first syncing bot should claim startup prewarm."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot})

    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ) as mock_get_room_threads_page,
        patch.object(
            router_bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
        patch.object(
            agent_bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
    ):
        await agent_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)
        await router_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        agent_bot.client,
        "!room:localhost",
        limit=20,
    )


@pytest.mark.asyncio
async def test_failed_room_claim_is_released_for_later_joined_bot(tmp_path: Path) -> None:
    """If one bot fails room-level prewarm, another joined bot can still claim the room later."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot})

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.background_tasks.logger.exception") as mock_background_logger_exception,
        patch.object(
            router_bot._conversation_cache,
            "prewarm_recent_room_threads",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ) as mock_router_prewarm,
        patch.object(
            agent_bot._conversation_cache,
            "prewarm_recent_room_threads",
            new=AsyncMock(return_value=None),
        ) as mock_agent_prewarm,
    ):
        await router_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)
        await agent_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)

    assert mock_router_prewarm.await_args.args == ("!room:localhost",)
    assert callable(mock_router_prewarm.await_args.kwargs["is_shutting_down"])
    assert mock_agent_prewarm.await_args.args == ("!room:localhost",)
    assert callable(mock_agent_prewarm.await_args.kwargs["is_shutting_down"])
    mock_background_logger_exception.assert_called_once()
    assert agent_bot.startup_thread_prewarm_registry._states["!room:localhost"] == "done"


@pytest.mark.asyncio
async def test_disabled_bot_does_not_block_enabled_bot_from_claiming_room(tmp_path: Path) -> None:
    """A bot with startup prewarm disabled should not block another joined bot from claiming the room."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"], startup_thread_prewarm=False),
                "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    disabled_bot = install_runtime_cache_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name="code",
                password=TEST_PASSWORD,
                display_name="Code",
                user_id="@mindroom_code:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
        ),
    )
    enabled_bot = install_runtime_cache_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name="research",
                password=TEST_PASSWORD,
                display_name="Research",
                user_id="@mindroom_research:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
        ),
    )
    disabled_bot.client = make_matrix_client_mock(user_id=disabled_bot.agent_user.user_id or "@mindroom_code:localhost")
    disabled_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    enabled_bot.client = make_matrix_client_mock(
        user_id=enabled_bot.agent_user.user_id or "@mindroom_research:localhost",
    )
    enabled_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    _bind_shared_runtime_support(orchestrator, {"code": disabled_bot, "research": enabled_bot})

    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ) as mock_get_room_threads_page,
        patch.object(
            enabled_bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(return_value=thread_history_result([], is_full_history=False)),
        ),
    ):
        await disabled_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=disabled_bot._runtime_view)
        await enabled_bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=enabled_bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        enabled_bot.client,
        "!room:localhost",
        limit=20,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_skips_failed_threads_and_logs_counts(tmp_path: Path) -> None:
    """Startup thread prewarm should fail open on individual thread refresh failures."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot._conversation_cache.logger = MagicMock()
    bot._conversation_cache._refresh_dispatch_thread_snapshot_for_startup_prewarm = AsyncMock(
        side_effect=[
            RuntimeError("boom"),
            thread_history_result([], is_full_history=False),
        ],
    )

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1),
        _thread_root_event("$thread-b:localhost", body="Thread B", origin_server_ts=2),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ),
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert [
        call.args[1]
        for call in bot._conversation_cache._refresh_dispatch_thread_snapshot_for_startup_prewarm.await_args_list
    ] == ["$thread-a:localhost", "$thread-b:localhost"]
    bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_thread_failed",
        room_id="!room:localhost",
        thread_id="$thread-a:localhost",
        error="boom",
    )
    bot._conversation_cache.logger.info.assert_any_call(
        "startup_thread_prewarm_complete",
        room_id="!room:localhost",
        threads_warmed=1,
        threads_failed=1,
        elapsed_ms=ANY,
    )


@pytest.mark.asyncio
async def test_non_router_hook_sender_prefers_current_bot_client(tmp_path: Path) -> None:
    """Non-router bots should send hook messages with their own Matrix client when available."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    bot.client.user_id = "@mindroom_code:localhost"
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock()
    router_bot.client.user_id = "@mindroom_router:localhost"
    orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    sent_clients: list[object] = []

    async def mock_send(client: object, _room_id: str, content: dict[str, object]) -> object:
        sent_clients.append(client)
        return delivered_matrix_event("$hook-event", content)

    sender = bot._hook_context_support.message_sender()
    assert sender is not None
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)

    with (
        patch("mindroom.hooks.sender.send_message_result", side_effect=mock_send),
    ):
        event_id = await sender("!room:localhost", "hello", None, "test-plugin:bot:ready", None)

    assert event_id == "$hook-event"
    assert sent_clients == [bot.client]
