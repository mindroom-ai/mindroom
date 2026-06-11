"""Unit tests for conversation identity and ingress envelope assembly.

These are characterization tests for ConversationResolver: they pin down
thread root resolution, reply-chain fallback, candidate demotion, target
building, and the per-turn cache scope so the planned refactor of this layer
has a direct safety net.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import SKIP_MENTIONS_KEY
from mindroom.conversation_resolver import ConversationResolver, ConversationResolverDeps
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.cache.thread_history_result import thread_history_result
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_matrix_client_mock,
    make_visible_message,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_ROOM_ID = "!test:localhost"
_SENDER = "@user:localhost"
_EVENT_ID = "$event:localhost"
_THREAD_ROOT = "$root:localhost"
_PARENT = "$parent:localhost"


@dataclass(frozen=True)
class _RuntimeStub:
    """Minimal SupportsClientConfig stand-in for resolver tests."""

    client: nio.AsyncClient | None
    config: Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Single-agent config bound to isolated runtime paths."""
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )


def _resolver(
    config: Config,
    *,
    conversation_cache: AsyncMock | None = None,
) -> ConversationResolver:
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    return ConversationResolver(
        ConversationResolverDeps(
            runtime=_RuntimeStub(client=make_matrix_client_mock(), config=config),
            logger=get_logger("test_conversation_resolver"),
            runtime_paths=runtime_paths,
            agent_name="general",
            matrix_id=registry.current_id("general"),
            conversation_cache=conversation_cache or make_conversation_cache_mock(),
        ),
    )


def _event(content: dict[str, Any], *, event_id: str = _EVENT_ID) -> nio.RoomMessageText:
    source = {
        "content": {"msgtype": "m.text", **content},
        "event_id": event_id,
        "sender": _SENDER,
        "origin_server_ts": 1_000_000,
        "room_id": _ROOM_ID,
        "type": "m.room.message",
    }
    return nio.RoomMessageText.from_dict(source)


def _threaded_event(body: str = "in thread") -> nio.RoomMessageText:
    return _event(
        {
            "body": body,
            "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD_ROOT},
        },
    )


def _reply_event(body: str = "a reply") -> nio.RoomMessageText:
    return _event(
        {
            "body": body,
            "m.relates_to": {"m.in_reply_to": {"event_id": _PARENT}},
        },
    )


def _room() -> nio.MatrixRoom:
    return nio.MatrixRoom(_ROOM_ID, "@mindroom_general:localhost")


@pytest.mark.asyncio
async def test_threaded_event_resolves_explicit_thread_root(config: Config) -> None:
    """An m.thread relation is authoritative for thread identity and the delivery target."""
    cache = make_conversation_cache_mock()
    resolver = _resolver(config, conversation_cache=cache)

    result = await resolver.extract_dispatch_context(_room(), _threaded_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _THREAD_ROOT
    assert result.context.requires_model_history_refresh is False
    assert result.thread_context is not None
    assert result.thread_context.stable_target.resolved_thread_id == _THREAD_ROOT
    cache.get_dispatch_thread_history.assert_awaited_once_with(_ROOM_ID, _THREAD_ROOT, caller_label="dispatch_context")


@pytest.mark.asyncio
async def test_reply_chain_falls_back_to_cached_thread_membership(config: Config) -> None:
    """A plain reply inherits the thread of its parent through the cached thread index."""
    cache = make_conversation_cache_mock()
    cache.get_thread_id_for_event = AsyncMock(
        side_effect=lambda _room_id, event_id: _THREAD_ROOT if event_id == _PARENT else None,
    )
    resolver = _resolver(config, conversation_cache=cache)

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _THREAD_ROOT
    assert result.thread_context is not None
    assert result.thread_context.stable_target.resolved_thread_id == _THREAD_ROOT


@pytest.mark.asyncio
async def test_reply_to_proven_thread_root_joins_that_thread(config: Config) -> None:
    """Replying to an event that provably has thread children resolves to that thread."""
    cache = make_conversation_cache_mock()
    cache.get_dispatch_thread_history = AsyncMock(
        return_value=thread_history_result(
            [make_visible_message(sender=_SENDER, body="child", event_id="$child:localhost")],
            is_full_history=True,
        ),
    )
    resolver = _resolver(config, conversation_cache=cache)

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is True
    assert result.context.thread_id == _PARENT
    assert [message.event_id for message in result.context.thread_history] == ["$child:localhost"]


@pytest.mark.asyncio
async def test_reply_to_plain_message_demotes_to_room_level(config: Config) -> None:
    """Replying to a childless event stays room-level with a room-level delivery target."""
    resolver = _resolver(config)

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is not None
    assert result.thread_context.candidate_thread_root_id is None
    # A reply event carries a relation, so per MSC3440 it cannot become a thread root itself.
    assert result.thread_context.stable_target.source_thread_id is None
    assert result.thread_context.stable_target.resolved_thread_id is None
    assert result.thread_context.stable_target.reply_to_event_id == _EVENT_ID


@pytest.mark.asyncio
async def test_reply_to_missing_parent_keeps_unproven_candidate(config: Config) -> None:
    """An unresolvable parent demotes to room level but keeps the candidate for replay safety."""
    cache = make_conversation_cache_mock()
    cache.get_event = AsyncMock(return_value=nio.RoomGetEventError("not found", "M_NOT_FOUND"))
    resolver = _resolver(config, conversation_cache=cache)

    result = await resolver.extract_dispatch_context(_room(), _reply_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is not None
    assert result.thread_context.candidate_thread_root_id == _PARENT
    assert result.thread_context.replay_guard_degraded is True
    # Unproven candidates must not adopt a thread target.
    assert result.thread_context.stable_target.resolved_thread_id is None


@pytest.mark.asyncio
async def test_room_thread_mode_skips_thread_resolution(tmp_path: Path) -> None:
    """Agents in room thread mode treat every message as room-level."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    cache = make_conversation_cache_mock()
    resolver = _resolver(config, conversation_cache=cache)

    result = await resolver.extract_dispatch_context(_room(), _threaded_event())

    assert result.context.is_thread is False
    assert result.context.thread_id is None
    assert result.thread_context is None
    cache.get_dispatch_thread_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_coalescing_thread_id_for_threaded_and_room_level_events(config: Config) -> None:
    """Coalescing scope follows canonical thread membership."""
    resolver = _resolver(config)

    assert await resolver.coalescing_thread_id(_room(), _threaded_event()) == _THREAD_ROOT
    assert await resolver.coalescing_thread_id(_room(), _event({"body": "plain"})) is None


@pytest.mark.asyncio
async def test_coalescing_thread_id_is_room_scoped_in_room_thread_mode(tmp_path: Path) -> None:
    """Room thread mode collapses coalescing scope to the room even for threaded events."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", thread_mode="room")}),
        test_runtime_paths(tmp_path),
    )
    resolver = _resolver(config)

    assert await resolver.coalescing_thread_id(_room(), _threaded_event()) is None


def test_build_message_target_for_thread_message(config: Config) -> None:
    """A known thread id resolves to thread-level delivery."""
    resolver = _resolver(config)

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
    )

    assert target.resolved_thread_id == _THREAD_ROOT
    assert target.session_id == f"{_ROOM_ID}:{_THREAD_ROOT}"


def test_build_message_target_starts_thread_at_rootable_room_message(config: Config) -> None:
    """A room-level message that can be a thread root becomes the new thread root."""
    resolver = _resolver(config)
    event = _event({"body": "plain"})

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=None,
        reply_to_event_id=_EVENT_ID,
        event_source=event.source,
    )

    assert target.source_thread_id is None
    assert target.resolved_thread_id == _EVENT_ID


def test_build_message_target_room_mode_override_stays_room_level(config: Config) -> None:
    """A room thread-mode override discards thread identity from the target."""
    resolver = _resolver(config)

    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
        thread_mode_override="room",
    )

    assert target.resolved_thread_id is None
    assert target.session_id == _ROOM_ID


@dataclass
class _ScopeTracker:
    entered: int = 0
    exited: int = 0


@pytest.mark.asyncio
async def test_turn_thread_cache_scope_wraps_conversation_cache_scope(config: Config) -> None:
    """The per-turn cache scope opens and closes the conversation cache turn scope."""
    cache = make_conversation_cache_mock()
    tracker = _ScopeTracker()

    @asynccontextmanager
    async def turn_scope() -> AsyncIterator[None]:
        tracker.entered += 1
        try:
            yield
        finally:
            tracker.exited += 1

    cache.turn_scope = turn_scope
    resolver = _resolver(config, conversation_cache=cache)

    async with resolver.turn_thread_cache_scope():
        assert tracker.entered == 1
        assert tracker.exited == 0

    assert tracker.exited == 1


@pytest.mark.asyncio
async def test_dispatch_context_extracts_agent_mentions(config: Config) -> None:
    """m.mentions on the inbound event resolve to configured agent identities."""
    registry = entity_identity_registry(config, runtime_paths_for(config))
    general_id = registry.current_id("general")
    resolver = _resolver(config)
    event = _event(
        {
            "body": "hello @general",
            "m.mentions": {"user_ids": [general_id.full_id, "@human:localhost"]},
        },
    )

    result = await resolver.extract_dispatch_context(_room(), event)

    assert result.context.am_i_mentioned is True
    assert [agent.full_id for agent in result.context.mentioned_agents] == [general_id.full_id]
    assert result.context.has_non_agent_mentions is True


@pytest.mark.asyncio
async def test_skip_mentions_metadata_suppresses_mention_extraction(config: Config) -> None:
    """The skip-mentions content flag disables mention handling for one event."""
    registry = entity_identity_registry(config, runtime_paths_for(config))
    general_id = registry.current_id("general")
    resolver = _resolver(config)
    event = _event(
        {
            "body": "hello @general",
            "m.mentions": {"user_ids": [general_id.full_id]},
            SKIP_MENTIONS_KEY: True,
        },
    )

    result = await resolver.extract_dispatch_context(_room(), event)

    assert result.context.am_i_mentioned is False
    assert result.context.mentioned_agents == []
    assert result.context.has_non_agent_mentions is False


@pytest.mark.asyncio
async def test_build_ingress_envelope_carries_event_identity(config: Config) -> None:
    """The lightweight ingress envelope mirrors the inbound event without thread extraction."""
    resolver = _resolver(config)
    event = _event({"body": "hello"})
    target = resolver.build_message_target(
        room_id=_ROOM_ID,
        thread_id=_THREAD_ROOT,
        reply_to_event_id=_EVENT_ID,
    )

    envelope = resolver.build_ingress_envelope(
        room_id=_ROOM_ID,
        event=event,
        requester_user_id=_SENDER,
        target=target,
    )

    assert envelope.source_event_id == _EVENT_ID
    assert envelope.room_id == _ROOM_ID
    assert envelope.target == target
    assert envelope.requester_id == _SENDER
    assert envelope.sender_id == _SENDER
    assert envelope.body == "hello"
    assert envelope.mentioned_agents == ()
    assert envelope.agent_name == "general"
    assert envelope.source_kind == "message"
