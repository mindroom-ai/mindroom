"""The hook capabilities a bot binds into hook contexts: its own live client first, then the router's."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, call, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import HOOK_SOURCE_KEY, ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME, SOURCE_KIND_KEY
from mindroom.hooks import EVENT_CONFIG_RELOADED, ConfigReloadedContext, HookContextSupport, HookRegistry, hook
from mindroom.hooks.registry import HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from tests.bot_helpers import _hook_plugin, make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.matrix.conversation_reads import ConversationReader
    from tests.runtime_fakes import ReadyRuntime


def _bot(tmp_path: Path, agent_name: str) -> AgentBot:
    """Return one bot, without a client yet, in a ready runtime of its own whose config also has the `code` agent."""
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    user = AgentMatrixUser(
        agent_name=agent_name,
        user_id=f"@mindroom_{agent_name}:localhost",
        display_name=agent_name.title(),
        password=TEST_PASSWORD,
    )
    return make_test_agent_bot(user, config=config, runtime_paths=runtime_paths_for(config))


def _capture_config_reloaded(orchestrator: ReadyRuntime) -> list[ConfigReloadedContext]:
    """Register a config:reloaded hook in ``orchestrator`` and return the contexts it receives."""
    captured: list[ConfigReloadedContext] = []

    @hook(EVENT_CONFIG_RELOADED)
    async def on_reload(ctx: ConfigReloadedContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [on_reload])])
    return captured


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a runtime-bound config for stand-in bots."""
    return bind_runtime_paths(Config(), test_runtime_paths(tmp_path))


def _hook_support(
    config: Config,
    agent_name: str,
    *,
    client: object | None = None,
    owner: object | None = None,
    poster: object | None = None,
    steward: object | None = None,
    reader: ConversationReader | None = None,
) -> HookContextSupport:
    """Return one bot's hook support in a stand-in runtime with that owner bot, and that poster and steward in every room."""
    orchestrator = SimpleNamespace(
        agent_bots={} if owner is None else {ROUTER_AGENT_NAME: owner},
        room_poster=lambda _room_id: poster,
        room_steward=lambda _room_id: steward,
    )
    return HookContextSupport(
        runtime=SimpleNamespace(client=client, config=config, orchestrator=orchestrator, runtime_started_at=0.0),
        runtime_paths=runtime_paths_for(config),
        agent_name=agent_name,
        hook_registry_state=HookRegistryState(HookRegistry.empty()),
        conversation_reader=reader or make_conversation_reader_mock(),
        logger=get_logger("tests.hook_context_support"),
    )


def _client(user_id: str = "@mindroom_code:localhost") -> AsyncMock:
    return make_matrix_client_mock(user_id=user_id)


def _peer(config: Config, agent_name: str, *, stopping: bool = False) -> SimpleNamespace:
    """Return a stand-in for another running bot with its own client, whose admission fence refuses once it stops."""
    support = _hook_support(config, agent_name, client=_client(f"@mindroom_{agent_name}:localhost"))

    @asynccontextmanager
    async def admission_fence() -> AsyncIterator[None]:
        if stopping:
            raise ResponseAdmissionRefusedError
        yield

    return SimpleNamespace(client=support.runtime.client, hook_context=support, admission_fence=admission_fence)


@pytest.mark.asyncio
async def test_hook_sender_reads_the_bots_client_when_it_sends(config: Config) -> None:
    """A bound sender follows the bot's own client over the room's poster: a replaced client sends, none sends nothing."""
    support = _hook_support(config, "code", client=_client(), poster=_peer(config, "general"))
    sender = support.bindings().message_sender
    assert sender is not None
    replacement = _client()
    support.runtime.client = replacement
    transport = AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event"))

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) == "$hook-event"
        support.runtime.client = None
        assert await sender("!room:localhost", "hello", None, "plugin:event", None) is None

    assert [sent.args[0] for sent in transport.await_args_list] == [replacement]


@pytest.mark.asyncio
async def test_sender_falls_back_to_room_poster(config: Config) -> None:
    """A bot whose client is gone sends as the poster of the target room, and sends nothing where that cannot send."""
    poster = _peer(config, "general")
    transport = AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event"))

    async def send(poster: object | None) -> str | None:
        sender = _hook_support(config, "code", poster=poster).bindings().message_sender
        assert sender is not None
        return await sender("!room:localhost", "hello", None, "plugin:event", None, mentions=["@a:localhost"])

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await send(poster) == "$hook-event"
        assert await send(None) is None
        assert await send(_peer(config, "general", stopping=True)) is None
        # A poster that stopped closed its client, which is a send that did not happen, not a failed hook.
        transport.side_effect = nio.LocalProtocolError("closed")
        assert await send(poster) is None

    assert [sent.args[0] for sent in transport.await_args_list] == [poster.client, poster.client]


@pytest.mark.asyncio
async def test_hook_sends_tag_their_source_thread_under_the_latest_event_and_keep_extra_content(config: Config) -> None:
    """A hook send tags its hook source, threads under the reader's latest event, and keeps the original sender."""
    reader = make_conversation_reader_mock()
    reader.latest_thread_event_id.return_value = "$latest"
    support = _hook_support(config, "code", client=_client(), reader=reader)
    transport = AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event"))

    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        event_id = await support.send_message(
            "!room:localhost",
            "hello",
            "$thread",
            "plugin:event",
            {"custom": "value", ORIGINAL_SENDER_KEY: "@user:localhost"},
        )

    assert event_id == "$hook-event"
    client, room_id, content = transport.await_args.args
    assert (client, room_id) == (support.runtime.client, "!room:localhost")
    assert content[SOURCE_KIND_KEY] == "hook"
    assert content[HOOK_SOURCE_KEY] == "plugin:event"
    assert content["custom"] == "value"
    assert content[ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert content["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }
    reader.latest_thread_event_id.assert_awaited_once_with(room_id="!room:localhost", thread_id="$thread")


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_in_room", [True, False])
async def test_state_putter_falls_back_to_steward(config: Config, agent_in_room: bool) -> None:
    """Only the owner's client backs matrix admin; room state asks the room's steward only when the bot's client fails."""
    steward = _peer(config, "general")
    router_client = steward.client
    router_client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Router Lobby"})
    agent_client = _client()
    if agent_in_room:
        agent_client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Agent Lobby"})
    else:
        agent_client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
        agent_client.room_put_state.return_value = nio.RoomPutStateError(message="forbidden")
    owner = _peer(config, ROUTER_AGENT_NAME)

    bindings = _hook_support(config, "code", client=agent_client, owner=owner, steward=steward).bindings()

    assert bindings.matrix_admin is not None
    assert bindings.matrix_admin.client is owner.client
    assert bindings.room_state_querier is not None
    assert bindings.room_state_putter is not None
    tags = {"tags": {"queued": True}}
    name = await bindings.room_state_querier("!room:localhost", "m.room.name", "")
    wrote = await bindings.room_state_putter("!room:localhost", "com.mindroom.thread.tags", "$thread", tags)
    assert (name, wrote) == ({"name": "Agent Lobby" if agent_in_room else "Router Lobby"}, True)
    get_state = call("!room:localhost", "m.room.name", "")
    put_state = call("!room:localhost", "com.mindroom.thread.tags", tags, state_key="$thread")
    assert agent_client.room_get_state_event.await_args_list == [get_state]
    assert agent_client.room_put_state.await_args_list == [put_state]
    assert router_client.room_get_state_event.await_args_list == ([] if agent_in_room else [get_state])
    assert router_client.room_put_state.await_args_list == ([] if agent_in_room else [put_state])


@pytest.mark.asyncio
async def test_state_without_a_usable_steward_is_a_failed_read_and_write(config: Config) -> None:
    """A room with no steward, or one that is stopping or closed its client, reads no state and writes none."""
    closed = _peer(config, "general")
    closed.client.room_get_state_event.side_effect = nio.LocalProtocolError("closed")
    closed.client.room_put_state.side_effect = nio.LocalProtocolError("closed")

    for steward in (None, _peer(config, "general", stopping=True), closed):
        bindings = _hook_support(config, "code", steward=steward).bindings()
        assert bindings.room_state_querier is not None
        assert bindings.room_state_putter is not None
        assert await bindings.room_state_querier("!room:localhost", "m.room.name", "") is None
        assert not await bindings.room_state_putter("!room:localhost", "m.room.name", "", {"name": "Lobby"})


def test_matrix_admin_none_without_owner(config: Config) -> None:
    """The owner's admin is bound to its own client, and nothing backs a bot's admin without a connected owner."""
    owner = _hook_support(config, ROUTER_AGENT_NAME)
    assert owner.bindings().matrix_admin is None
    assert _hook_support(config, "code", client=_client()).bindings().matrix_admin is None
    disconnected = SimpleNamespace(hook_context=owner)
    assert _hook_support(config, "code", client=_client(), owner=disconnected).bindings().matrix_admin is None

    owner.runtime.client = _client("@mindroom_router:localhost")
    admin = owner.bindings().matrix_admin

    assert admin is not None
    assert admin.client is owner.runtime.client


@pytest.mark.asyncio
async def test_hook_capabilities_read_the_bots_conversations(config: Config) -> None:
    """A bound sender finds the thread's latest event, and the snapshot reader reads, through the bot's conversations."""
    reader = make_conversation_reader_mock()
    reader.latest_thread_event_id.return_value = "$latest"
    bindings = _hook_support(config, "code", client=_client(), reader=reader).bindings()
    assert bindings.message_sender is not None
    assert bindings.agent_message_snapshot_reader is not None

    with patch(
        "mindroom.hooks.sender.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event")),
    ):
        await bindings.message_sender("!room:localhost", "hello", "$thread", "plugin:event", None)
    await bindings.agent_message_snapshot_reader(room_id="!room:localhost", thread_id=None, sender="@a:localhost")

    reader.latest_thread_event_id.assert_awaited_once_with(room_id="!room:localhost", thread_id="$thread")
    reader.read.assert_awaited_once()


@pytest.mark.asyncio
async def test_config_reloaded_hooks_get_the_service_capabilities_and_the_runtimes_hook_lifetime(
    tmp_path: Path,
) -> None:
    """config:reloaded sends as each room's poster and holds the runtime's hook lifetime, which outlives every bot."""
    router = _bot(tmp_path, ROUTER_AGENT_NAME)
    router.client = make_matrix_client_mock(user_id=router.agent_user.user_id)
    router.client.rooms = {"!room:localhost": SimpleNamespace()}
    router.running = True
    orchestrator = cast("ReadyRuntime", router.orchestrator)
    captured = _capture_config_reloaded(orchestrator)
    plan = ConfigUpdatePlan(new_config=orchestrator.config, start=frozenset({"new"}), remove=frozenset({"old"}))
    transport = AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event"))

    await orchestrator._emit_config_reloaded(plan, {"code", "new"})
    [ctx] = captured
    with patch("mindroom.hooks.sender.send_matrix_message", new=transport):
        assert await ctx.send_message("!room:localhost", "reloaded") == "$hook-event"

    assert [sent.args[0] for sent in transport.await_args_list] == [router.client]
    assert ctx.config is plan.new_config
    assert ctx.runtime_started_at == router.hook_context.runtime.runtime_started_at
    assert ctx.matrix_admin is not None
    assert ctx.matrix_admin.client is router.client
    assert ctx.room_state_querier is not None
    assert ctx.room_state_putter is not None
    assert (ctx.changed_entities, ctx.added_entities, ctx.removed_entities) == (("code", "new"), ("new",), ("old",))
    assert ctx.correlation_id.startswith("config-reload:")
    await router.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    assert ctx.is_active()


@pytest.mark.asyncio
async def test_config_reloaded_runs_service_scoped_hooks_and_no_router_filtered_one(config: Config) -> None:
    """A reload runs a service-scoped hook, and a hook still filtered to the router stays as silent as it was."""
    orchestrator = ready_runtime(config, runtime_paths_for(config))
    calls: list[str] = []

    @hook(EVENT_CONFIG_RELOADED, agents=(ROUTER_AGENT_NAME,))
    async def legacy(ctx: ConfigReloadedContext) -> None:
        calls.append(f"legacy:{ctx.event_name}")

    @hook(EVENT_CONFIG_RELOADED, scope="service")
    async def service(ctx: ConfigReloadedContext) -> None:
        calls.append(f"service:{ctx.event_name}")

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("p", [legacy, service])])

    await orchestrator._emit_config_reloaded(ConfigUpdatePlan(new_config=orchestrator.config), set())

    assert calls == ["service:config:reloaded"]


@pytest.mark.asyncio
async def test_config_reloaded_hooks_without_a_bot_get_no_matrix_capabilities(config: Config) -> None:
    """With no bot to send or hold state, config:reloaded still fires, stamped with the reload time; its helpers do nothing."""
    orchestrator = ready_runtime(config, runtime_paths_for(config))
    orchestrator.steward = None
    captured = _capture_config_reloaded(orchestrator)
    before = time.time()

    await orchestrator._emit_config_reloaded(ConfigUpdatePlan(new_config=orchestrator.config), set())

    [ctx] = captured
    assert ctx.runtime_started_at is not None
    assert before <= ctx.runtime_started_at <= time.time()
    assert await ctx.send_message("!room:localhost", "reloaded") is None
    assert ctx.matrix_admin is None
    assert await ctx.query_room_state("!room:localhost", "m.room.name", "") is None
    assert not await ctx.put_room_state("!room:localhost", "m.room.name", "", {"name": "Lobby"})
    assert ctx.agent_message_snapshot_reader is None
    assert ctx.is_active()
