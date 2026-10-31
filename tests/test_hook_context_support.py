"""The hook capabilities a bot binds into hook contexts: its own live client first, then the router's."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_CONFIG_RELOADED, ConfigReloadedContext, HookContextSupport, HookRegistry, hook
from mindroom.hooks.registry import HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from tests.bot_helpers import _hook_plugin, make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from tests.runtime_fakes import ReadyRuntime


def _user(agent_name: str) -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=agent_name,
        user_id=f"@mindroom_{agent_name}:localhost",
        display_name=agent_name.title(),
        password=TEST_PASSWORD,
    )


def _bot(tmp_path: Path, agent_name: str) -> AgentBot:
    """Return one bot, without a client yet, in a ready runtime of its own whose config also has the `code` agent."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    return make_test_agent_bot(_user(agent_name), config=config, runtime_paths=runtime_paths_for(config))


def _capture_config_reloaded(orchestrator: ReadyRuntime) -> list[ConfigReloadedContext]:
    """Register a config:reloaded hook in ``orchestrator`` and return the contexts it receives."""
    captured: list[ConfigReloadedContext] = []

    @hook(EVENT_CONFIG_RELOADED)
    async def on_reload(ctx: ConfigReloadedContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [on_reload])])
    return captured


def _sent_through() -> tuple[list[object], AsyncMock]:
    """Return the clients hook sends went through, and the transport that records them."""
    clients: list[object] = []

    async def send(client: object, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        clients.append(client)
        return delivered_matrix_event("$hook-event", content)

    return clients, AsyncMock(side_effect=send)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a runtime-bound config for stand-in bots."""
    return bind_runtime_paths(Config(), test_runtime_paths(tmp_path))


def _hook_support(
    config: Config,
    agent_name: str,
    *,
    client: object | None = None,
    router: HookContextSupport | None = None,
    readers: list[ConversationReader] | None = None,
) -> HookContextSupport:
    """Return one bot's hook support over a stand-in runtime whose orchestrator offers ``router`` as the router's.

    The support reads the last of ``readers``, as a bot reads the reader its latest login built.
    """

    def router_hook_context() -> HookContextSupport | None:
        assert agent_name != ROUTER_AGENT_NAME, "the router never asks for the router's hook support"
        return router

    readers = readers if readers is not None else [make_conversation_reader_mock()]
    return HookContextSupport(
        runtime=SimpleNamespace(
            client=client,
            config=config,
            orchestrator=SimpleNamespace(router_hook_context=router_hook_context),
            runtime_started_at=0.0,
        ),
        runtime_paths=runtime_paths_for(config),
        agent_name=agent_name,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        conversation_reader=lambda: readers[-1],
        logger=get_logger("tests.hook_context_support"),
    )


def _client(user_id: str = "@mindroom_code:localhost") -> AsyncMock:
    return make_matrix_client_mock(user_id=user_id)


@pytest.mark.asyncio
async def test_hook_sender_reads_the_bots_client_when_it_sends(config: Config) -> None:
    """A sender bound into a hook context keeps following the bot's client: a replaced client sends, none sends nothing."""
    support = _hook_support(config, "code", client=_client())
    sender = support.bindings().message_sender
    assert sender is not None
    replacement = _client()
    support.runtime.client = replacement
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"
        support.runtime.client = None
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) is None

    assert clients == [replacement]


@pytest.mark.asyncio
async def test_an_agent_without_a_client_sends_hook_messages_through_the_router(config: Config) -> None:
    """An agent whose client is gone binds the router's sender, which sends with the router's client."""
    router = _hook_support(config, ROUTER_AGENT_NAME, client=_client("@mindroom_router:localhost"))
    sender = _hook_support(config, "code", router=router).bindings().message_sender
    assert sender is not None
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"

    assert clients == [router.runtime.client]


@pytest.mark.asyncio
async def test_a_router_without_a_client_binds_a_sender_that_works_once_it_connects(config: Config) -> None:
    """A hook context bound while the router has no client sends through it after that same router connects."""
    router = _hook_support(config, ROUTER_AGENT_NAME)
    sender = router.bindings().message_sender
    assert sender is not None
    router.runtime.client = _client("@mindroom_router:localhost")
    clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"

    assert clients == [router.runtime.client]


@pytest.mark.asyncio
async def test_agent_hooks_use_the_routers_matrix_admin_and_fall_back_to_its_room_state(config: Config) -> None:
    """Only the router's client backs matrix admin; room state tries the agent's own client first."""
    router_client = _client("@mindroom_router:localhost")
    router_client.room_get_state_event.return_value = AsyncMock(content={"name": "Router Lobby"})
    agent_client = _client()
    agent_client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
    router = _hook_support(config, ROUTER_AGENT_NAME, client=router_client)

    bindings = _hook_support(config, "code", client=agent_client, router=router).bindings()

    assert bindings.matrix_admin is not None
    assert bindings.matrix_admin.client is router_client
    assert bindings.room_state_querier is not None
    assert await bindings.room_state_querier("!room:localhost", "m.room.name", "") == {"name": "Router Lobby"}
    agent_client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")


def test_the_router_builds_its_matrix_admin_from_its_own_client(config: Config) -> None:
    """The router's admin is bound to its own client, and nothing backs any bot's admin while it has none."""
    router = _hook_support(config, ROUTER_AGENT_NAME)
    assert router.bindings().matrix_admin is None
    assert _hook_support(config, "code", client=_client(), router=router).bindings().matrix_admin is None
    assert _hook_support(config, "code", client=_client()).bindings().matrix_admin is None

    router.runtime.client = _client("@mindroom_router:localhost")
    admin = router.bindings().matrix_admin

    assert admin is not None
    assert admin.client is router.runtime.client


@pytest.mark.asyncio
async def test_retained_hook_capabilities_read_the_bots_current_conversations(config: Config) -> None:
    """A bound sender and snapshot reader read the conversation reader the bot has now, not the one it had then."""
    readers = [make_conversation_reader_mock()]
    bindings = _hook_support(config, "code", client=_client(), readers=readers).bindings()
    assert bindings.message_sender is not None
    assert bindings.agent_message_snapshot_reader is not None
    current_reader = make_conversation_reader_mock()
    current_reader.latest_thread_event_id.return_value = "$latest"
    readers.append(current_reader)
    _clients, transport = _sent_through()

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        await bindings.message_sender("!room:localhost", "hello", "$thread", "plugin:event", None)
    await bindings.agent_message_snapshot_reader(room_id="!room:localhost", thread_id=None, sender="@a:localhost")

    current_reader.latest_thread_event_id.assert_awaited_once_with(room_id="!room:localhost", thread_id="$thread")
    current_reader.read.assert_awaited_once()
    readers[0].latest_thread_event_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_reloaded_hooks_get_the_routers_capabilities_and_the_runtimes_hook_lifetime(
    tmp_path: Path,
) -> None:
    """config:reloaded binds the router's client capabilities but the runtime's hook lifetime, which outlives the router."""
    router = _bot(tmp_path, ROUTER_AGENT_NAME)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    orchestrator = cast("ReadyRuntime", router.orchestrator)
    captured = _capture_config_reloaded(orchestrator)
    plan = ConfigUpdatePlan(new_config=orchestrator.config, start=frozenset({"new"}), remove=frozenset({"old"}))
    clients, transport = _sent_through()

    await orchestrator._emit_config_reloaded(plan, {"code", "new"})
    [ctx] = captured
    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await ctx.send_message("!room:localhost", "reloaded") == "$hook-event"

    assert clients == [router.client]
    assert ctx.config is plan.new_config
    assert ctx.runtime_started_at == router.hook_context.runtime.runtime_started_at
    assert ctx.matrix_admin is not None
    assert ctx.matrix_admin.client is router.client
    assert ctx.room_state_querier is not None
    assert ctx.room_state_putter is not None
    assert ctx.agent_message_snapshot_reader is not None
    assert (ctx.changed_entities, ctx.added_entities, ctx.removed_entities) == (("code", "new"), ("new",), ("old",))
    assert ctx.correlation_id.startswith("config-reload:")
    await router.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    assert ctx.is_active()


@pytest.mark.asyncio
async def test_config_reloaded_hooks_without_a_router_get_no_matrix_capabilities(config: Config) -> None:
    """Without a router bot, config:reloaded still fires, stamped with the reload time and no Matrix helpers."""
    orchestrator = ready_runtime(config, runtime_paths_for(config))
    captured = _capture_config_reloaded(orchestrator)
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
    assert ctx.is_active()


@pytest.mark.asyncio
async def test_retained_hook_capabilities_read_the_bots_conversations_after_its_identity_changes(
    tmp_path: Path,
) -> None:
    """A sender and snapshot reader bound before login switched the bot's Matrix user read the current principal."""
    agent = _bot(tmp_path, "code")
    agent.client = make_matrix_client_mock(user_id=agent.agent_user.user_id)
    bindings = agent.hook_context.bindings()
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
