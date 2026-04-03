"""Tests for hook room state querier/putter and HookContext.query/put_room_state."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    AgentLifecycleContext,
    HookContext,
    HookRoomStatePutter,
    HookRoomStateQuerier,
    build_hook_room_state_putter,
    build_hook_room_state_querier,
)
from mindroom.hooks.types import EVENT_MESSAGE_RECEIVED
from mindroom.logging_config import get_logger
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: object) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="test", id="test-model")}),
        runtime_paths,
    )


def _hook_context(
    tmp_path: object,
    *,
    room_state_querier: HookRoomStateQuerier | None = None,
    room_state_putter: HookRoomStatePutter | None = None,
) -> HookContext:
    config = _config(tmp_path)
    return HookContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="test-plugin",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_room_state").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-room-state",
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
    )


def _lifecycle_context(
    tmp_path: object,
    *,
    room_state_querier: HookRoomStateQuerier | None = None,
) -> AgentLifecycleContext:
    config = _config(tmp_path)
    return AgentLifecycleContext(
        event_name=EVENT_AGENT_STARTED,
        plugin_name="test-plugin",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_room_state").bind(event_name=EVENT_AGENT_STARTED),
        correlation_id="corr-lifecycle",
        entity_name="code",
        entity_type="agent",
        rooms=("!room1:localhost", "!room2:localhost"),
        matrix_user_id="@mindroom_code:localhost",
        room_state_querier=room_state_querier,
    )


# -- build_hook_room_state_querier tests --


@pytest.mark.asyncio
async def test_querier_with_state_key_returns_content() -> None:
    """Single state event lookup returns the event content."""
    client = AsyncMock(spec=nio.AsyncClient)
    resp = MagicMock(spec=nio.RoomGetStateEventResponse)
    resp.content = {"tags": {"pending-restart": True}}
    client.room_get_state_event.return_value = resp

    querier = build_hook_room_state_querier(client)
    result = await querier("!room:localhost", "com.mindroom.thread.tags", "$thread1")

    assert result == {"tags": {"pending-restart": True}}
    client.room_get_state_event.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        "$thread1",
    )


@pytest.mark.asyncio
async def test_querier_with_state_key_error_returns_none() -> None:
    """State event lookup returns None on Matrix error responses."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="not found")

    querier = build_hook_room_state_querier(client)
    result = await querier("!room:localhost", "com.mindroom.thread.tags", "$thread1")

    assert result is None


@pytest.mark.asyncio
async def test_querier_without_state_key_returns_filtered_dict() -> None:
    """Querying without state_key returns {state_key: content} for matching events."""
    client = AsyncMock(spec=nio.AsyncClient)
    resp = MagicMock(spec=nio.RoomGetStateResponse)
    resp.events = [
        {"type": "com.mindroom.thread.tags", "state_key": "$t1", "content": {"tags": {"pending-restart": True}}},
        {"type": "com.mindroom.thread.tags", "state_key": "$t2", "content": {"tags": {"wip": True}}},
        {"type": "m.room.name", "state_key": "", "content": {"name": "Lobby"}},
    ]
    client.room_get_state.return_value = resp

    querier = build_hook_room_state_querier(client)
    result = await querier("!room:localhost", "com.mindroom.thread.tags", None)

    assert result == {
        "$t1": {"tags": {"pending-restart": True}},
        "$t2": {"tags": {"wip": True}},
    }
    client.room_get_state.assert_awaited_once_with("!room:localhost")


@pytest.mark.asyncio
async def test_querier_without_state_key_error_returns_none() -> None:
    """Full state query returns None on Matrix error responses."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_state.return_value = nio.RoomGetStateError(message="forbidden")

    querier = build_hook_room_state_querier(client)
    result = await querier("!room:localhost", "com.mindroom.thread.tags", None)

    assert result is None


@pytest.mark.asyncio
async def test_querier_without_state_key_no_matches_returns_empty() -> None:
    """Full state query with no matching event type returns empty dict."""
    client = AsyncMock(spec=nio.AsyncClient)
    resp = MagicMock(spec=nio.RoomGetStateResponse)
    resp.events = [
        {"type": "m.room.name", "state_key": "", "content": {"name": "Lobby"}},
    ]
    client.room_get_state.return_value = resp

    querier = build_hook_room_state_querier(client)
    result = await querier("!room:localhost", "com.mindroom.thread.tags", None)

    assert result == {}


@pytest.mark.asyncio
async def test_querier_propagates_transport_exception() -> None:
    """Transport exceptions should propagate instead of being converted to None."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_get_state_event.side_effect = RuntimeError("boom")

    querier = build_hook_room_state_querier(client)

    with pytest.raises(RuntimeError, match="boom"):
        await querier("!room:localhost", "com.mindroom.thread.tags", "$thread1")


# -- HookContext.query_room_state tests --


@pytest.mark.asyncio
async def test_hook_context_query_room_state_without_querier_returns_none(tmp_path: object) -> None:
    """query_room_state logs a warning and returns None when no querier is bound."""
    ctx = _hook_context(tmp_path)
    ctx.logger = MagicMock()

    result = await ctx.query_room_state("!room:localhost", "com.mindroom.thread.tags")

    assert result is None
    ctx.logger.warning.assert_called_once_with("No room state querier available")


@pytest.mark.asyncio
async def test_hook_context_query_room_state_delegates_to_querier(tmp_path: object) -> None:
    """query_room_state forwards call to bound querier."""
    querier: AsyncMock = AsyncMock(return_value={"$t1": {"tags": {"pending-restart": True}}})
    ctx = _hook_context(tmp_path, room_state_querier=querier)

    result = await ctx.query_room_state("!room:localhost", "com.mindroom.thread.tags")

    assert result == {"$t1": {"tags": {"pending-restart": True}}}
    querier.assert_awaited_once_with("!room:localhost", "com.mindroom.thread.tags", None)


@pytest.mark.asyncio
async def test_hook_context_query_room_state_with_state_key(tmp_path: object) -> None:
    """query_room_state passes state_key through to querier."""
    querier: AsyncMock = AsyncMock(return_value={"tags": {"pending-restart": True}})
    ctx = _hook_context(tmp_path, room_state_querier=querier)

    result = await ctx.query_room_state("!room:localhost", "com.mindroom.thread.tags", "$thread1")

    assert result == {"tags": {"pending-restart": True}}
    querier.assert_awaited_once_with("!room:localhost", "com.mindroom.thread.tags", "$thread1")


def test_hook_context_state_root_rejects_invalid_plugin_name(tmp_path: Path) -> None:
    """state_root should reject invalid plugin names instead of escaping plugin storage."""
    config = _config(tmp_path)
    ctx = HookContext(
        event_name=EVENT_MESSAGE_RECEIVED,
        plugin_name="../../escaped",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests.hook_room_state").bind(event_name=EVENT_MESSAGE_RECEIVED),
        correlation_id="corr-room-state",
    )

    with pytest.raises(ValueError, match="Invalid plugin name"):
        _ = ctx.state_root


# -- AgentLifecycleContext inherits room_state_querier --


@pytest.mark.asyncio
async def test_lifecycle_context_has_room_state_querier(tmp_path: object) -> None:
    """AgentLifecycleContext should accept and expose room_state_querier."""
    querier: AsyncMock = AsyncMock(return_value=None)
    ctx = _lifecycle_context(tmp_path, room_state_querier=querier)

    assert ctx.room_state_querier is querier


@pytest.mark.asyncio
async def test_lifecycle_context_query_room_state_delegates(tmp_path: object) -> None:
    """AgentLifecycleContext.query_room_state should use the bound querier."""
    querier: AsyncMock = AsyncMock(return_value={"$t1": {"tags": {}}})
    ctx = _lifecycle_context(tmp_path, room_state_querier=querier)

    result = await ctx.query_room_state("!room1:localhost", "com.mindroom.thread.tags")

    assert result == {"$t1": {"tags": {}}}
    querier.assert_awaited_once_with("!room1:localhost", "com.mindroom.thread.tags", None)


# -- build_hook_room_state_putter tests --


@pytest.mark.asyncio
async def test_putter_success_returns_true() -> None:
    """Successful room_put_state returns True."""
    client = AsyncMock(spec=nio.AsyncClient)
    resp = MagicMock(spec=nio.RoomPutStateResponse)
    client.room_put_state.return_value = resp

    putter = build_hook_room_state_putter(client)
    result = await putter("!room:localhost", "com.mindroom.thread.tags", "$thread1", {"tags": {}})

    assert result is True
    client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {}},
        state_key="$thread1",
    )


@pytest.mark.asyncio
async def test_putter_error_returns_false() -> None:
    """Matrix error responses from room_put_state return False."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_put_state.return_value = nio.RoomPutStateError(message="forbidden")

    putter = build_hook_room_state_putter(client)
    result = await putter("!room:localhost", "com.mindroom.thread.tags", "$thread1", {"tags": {}})

    assert result is False


@pytest.mark.asyncio
async def test_putter_propagates_transport_exception() -> None:
    """Transport exceptions should propagate instead of being converted to False."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.room_put_state.side_effect = RuntimeError("boom")

    putter = build_hook_room_state_putter(client)

    with pytest.raises(RuntimeError, match="boom"):
        await putter("!room:localhost", "com.mindroom.thread.tags", "$thread1", {"tags": {}})


# -- HookContext.put_room_state tests --


@pytest.mark.asyncio
async def test_hook_context_put_room_state_without_putter_returns_false(tmp_path: object) -> None:
    """put_room_state logs a warning and returns False when no putter is bound."""
    ctx = _hook_context(tmp_path)
    ctx.logger = MagicMock()

    result = await ctx.put_room_state("!room:localhost", "com.mindroom.thread.tags", "$t1", {"tags": {}})

    assert result is False
    ctx.logger.warning.assert_called_once_with("No room state putter available")


@pytest.mark.asyncio
async def test_hook_context_put_room_state_delegates_to_putter(tmp_path: object) -> None:
    """put_room_state forwards call to bound putter."""
    putter: AsyncMock = AsyncMock(return_value=True)
    ctx = _hook_context(tmp_path, room_state_putter=putter)

    result = await ctx.put_room_state("!room:localhost", "com.mindroom.thread.tags", "$t1", {"tags": {"wip": True}})

    assert result is True
    putter.assert_awaited_once_with("!room:localhost", "com.mindroom.thread.tags", "$t1", {"tags": {"wip": True}})
