"""Tests for the hook capabilities a bot binds into hook contexts: its own client first, then the router's."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_CONFIG_RELOADED, ConfigReloadedContext, HookRegistry, hook
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from tests.bot_helpers import _hook_plugin, make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.hooks import HookContextSupport
    from tests.runtime_fakes import ReadyRuntime


def _user(agent_name: str) -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=agent_name,
        user_id=f"@mindroom_{agent_name}:localhost",
        display_name=agent_name.title(),
        password=TEST_PASSWORD,
    )


def _router_and_agent(tmp_path: Path) -> tuple[AgentBot, AgentBot]:
    """Return the router and the `code` agent, both in one ready runtime and neither with a client yet."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    router = make_test_agent_bot(_user(ROUTER_AGENT_NAME), config=config, runtime_paths=runtime_paths_for(config))
    agent = make_test_agent_bot(_user("code"), orchestrator=cast("ReadyRuntime", router.orchestrator))
    return router, agent


def _support(bot: AgentBot) -> HookContextSupport:
    return bot._hook_context_support


def _sent_through() -> tuple[list[object], AsyncMock]:
    """Return the clients hook sends went through, and the transport that records them."""
    clients: list[object] = []

    async def send(client: object, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        clients.append(client)
        return delivered_matrix_event("$hook-event", content)

    return clients, AsyncMock(side_effect=send)


@pytest.mark.asyncio
async def test_hook_sender_reads_the_bots_client_when_it_sends(tmp_path: Path) -> None:
    """A sender bound into a hook context keeps following the bot's client: a replaced client sends, none sends nothing."""
    _router, agent = _router_and_agent(tmp_path)
    agent.client = make_matrix_client_mock(user_id=agent.agent_user.user_id)
    sender = _support(agent).bindings().message_sender
    assert sender is not None
    replacement = make_matrix_client_mock(user_id=agent.agent_user.user_id)
    agent.client = replacement
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"
        agent.client = None
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) is None

    assert clients == [replacement]


@pytest.mark.asyncio
async def test_an_agent_without_a_client_sends_hook_messages_through_the_router(tmp_path: Path) -> None:
    """An agent whose client is gone binds the router's sender, which sends with the router's client."""
    router, agent = _router_and_agent(tmp_path)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    sender = _support(agent).bindings().message_sender
    assert sender is not None
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"

    assert clients == [router.client]


@pytest.mark.asyncio
async def test_agent_hooks_use_the_routers_matrix_admin_and_room_state(tmp_path: Path) -> None:
    """Only the router's client backs matrix admin; room state falls back to it when the agent has no client."""
    router, agent = _router_and_agent(tmp_path)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    router.client.room_get_state_event.return_value = AsyncMock(content={"name": "Lobby"})

    bindings = _support(agent).bindings()

    assert bindings.matrix_admin is not None
    assert bindings.matrix_admin.client is router.client
    assert bindings.room_state_querier is not None
    assert await bindings.room_state_querier("!room:localhost", "m.room.name", "") == {"name": "Lobby"}
    router.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")


def test_the_router_builds_its_matrix_admin_from_its_own_client(tmp_path: Path) -> None:
    """The router's admin is bound to its own client, and nothing backs it while it has none."""
    router, agent = _router_and_agent(tmp_path)
    assert _support(router).bindings().matrix_admin is None
    assert _support(agent).bindings().matrix_admin is None

    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    admin = _support(router).bindings().matrix_admin

    assert admin is not None
    assert admin.client is router.client


@pytest.mark.asyncio
async def test_config_reloaded_hooks_get_the_routers_capabilities_and_the_runtimes_hook_lifetime(
    tmp_path: Path,
) -> None:
    """config:reloaded binds the router's client capabilities but the runtime's hook lifetime, not the router's."""
    router, _agent = _router_and_agent(tmp_path)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    orchestrator = cast("ReadyRuntime", router.orchestrator)
    captured: list[ConfigReloadedContext] = []

    @hook(EVENT_CONFIG_RELOADED)
    async def on_reload(ctx: ConfigReloadedContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [on_reload])])
    plan = ConfigUpdatePlan(new_config=orchestrator.config, start=frozenset({"new"}), remove=frozenset({"old"}))
    clients, transport = _sent_through()

    await orchestrator._emit_config_reloaded(plan, {"code", "new"})
    [ctx] = captured
    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await ctx.send_message("!room:localhost", "reloaded") == "$hook-event"

    assert clients == [router.client]
    assert ctx.config is plan.new_config
    assert ctx.runtime_started_at == router.runtime_started_at
    assert ctx.matrix_admin is not None
    assert ctx.matrix_admin.client is router.client
    assert ctx.room_state_querier is not None
    assert ctx.room_state_putter is not None
    assert ctx.agent_message_snapshot_reader is not None
    assert ctx._hook_registry_state is orchestrator._hook_lifetime
    assert ctx.is_active()
    assert (ctx.changed_entities, ctx.added_entities, ctx.removed_entities) == (("code", "new"), ("new",), ("old",))
    assert ctx.correlation_id.startswith("config-reload:")


@pytest.mark.asyncio
async def test_config_reloaded_hooks_without_a_router_get_no_matrix_capabilities(tmp_path: Path) -> None:
    """Without a router bot, config:reloaded still fires, stamped with the reload time and no Matrix helpers."""
    router, _agent = _router_and_agent(tmp_path)
    orchestrator = ready_runtime(router.config, router.runtime_paths)
    captured: list[ConfigReloadedContext] = []

    @hook(EVENT_CONFIG_RELOADED)
    async def on_reload(ctx: ConfigReloadedContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [on_reload])])
    before = time.time()

    await orchestrator._emit_config_reloaded(ConfigUpdatePlan(new_config=orchestrator.config), set())

    [ctx] = captured
    assert ctx.runtime_started_at is not None
    assert before <= ctx.runtime_started_at <= time.time()
    assert ctx.message_sender is None
    assert ctx.matrix_admin is None
    assert ctx.room_state_querier is None
    assert ctx.room_state_putter is None
    assert ctx.agent_message_snapshot_reader is None
    assert ctx._hook_registry_state is orchestrator._hook_lifetime


@pytest.mark.asyncio
async def test_a_router_without_a_client_binds_a_sender_that_works_once_it_connects(tmp_path: Path) -> None:
    """A hook context bound while the router has no client sends through it after that same router connects."""
    router, _agent = _router_and_agent(tmp_path)
    sender = _support(router).bindings().message_sender
    assert sender is not None
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"

    assert clients == [router.client]


@pytest.mark.asyncio
async def test_retained_hook_capabilities_read_the_bots_conversations_after_its_identity_changes(
    tmp_path: Path,
) -> None:
    """A sender and snapshot reader bound before login switched the bot's Matrix user read the current principal."""
    _router, agent = _router_and_agent(tmp_path)
    agent.client = make_matrix_client_mock(user_id=agent.agent_user.user_id)
    bindings = _support(agent).bindings()
    assert bindings.message_sender is not None
    assert bindings.agent_message_snapshot_reader is not None
    before_login = agent.matrix_id
    reader_before_login = agent.conversation_reader
    agent.agent_user.user_id = "@mindroom_code_renamed:localhost"
    agent._rebuild_runtime_components_after_login_if_identity_changed(before_login)
    current_reader = agent.conversation_reader
    assert current_reader is not reader_before_login
    _clients, transport = _sent_through()

    with (
        patch.object(ConversationReader, "latest_thread_event_id", autospec=True, return_value="$latest") as latest,
        patch.object(ConversationReader, "read", autospec=True, return_value=SimpleNamespace(messages=[])) as read,
        patch("mindroom.hooks.sender.send_matrix_message", new=transport),
    ):
        await bindings.message_sender("!room:localhost", "hello", "$thread", "plugin:event", None)
        await bindings.agent_message_snapshot_reader(room_id="!room:localhost", thread_id=None, sender="@a:localhost")

    assert [call.args[0] for call in latest.await_args_list] == [current_reader]
    assert [call.args[0] for call in read.await_args_list] == [current_reader]


@pytest.mark.asyncio
async def test_config_reloaded_contexts_stay_active_after_the_router_stops(tmp_path: Path) -> None:
    """config:reloaded contexts belong to the runtime's hook lifetime, so the router's stop does not end them."""
    router, _agent = _router_and_agent(tmp_path)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    orchestrator = cast("ReadyRuntime", router.orchestrator)
    captured: list[ConfigReloadedContext] = []

    @hook(EVENT_CONFIG_RELOADED)
    async def on_reload(ctx: ConfigReloadedContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [on_reload])])
    await orchestrator._emit_config_reloaded(ConfigUpdatePlan(new_config=orchestrator.config), set())

    await router.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    [ctx] = captured
    assert ctx.is_active()
