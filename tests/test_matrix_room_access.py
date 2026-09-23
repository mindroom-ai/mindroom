"""Tests for applying membership room policy to Matrix rooms."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, Mock

import nio
import pytest

from mindroom.access_policy import resolve_room_policy
from mindroom.matrix import client_room_admin
from mindroom.matrix import rooms as matrix_rooms
from tests.access_schema_support import membership_config
from tests.conftest import runtime_paths_for

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.access_policy import EffectiveRoomPolicy


@pytest.mark.asyncio
async def test_configure_managed_room_access_applies_effective_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Join and directory state must come from the resolved room policy."""
    config = membership_config(
        tmp_path,
        agent_rooms=["lobby"],
        room_defaults={"join_policy": "knock", "listed": True},
    )
    policy = resolve_room_policy(config, "lobby")
    ensure_join_rule = AsyncMock(return_value=True)
    ensure_visibility = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", ensure_join_rule)
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", ensure_visibility)

    result = await matrix_rooms._configure_managed_room_access(
        client=AsyncMock(),
        room_key="lobby",
        room_id="!lobby:example.com",
        room_policy=policy,
        context="test",
    )

    assert result is True
    ensure_join_rule.assert_awaited_once_with(
        ANY,
        "!lobby:example.com",
        "knock",
        snapshot=None,
    )
    ensure_visibility.assert_awaited_once_with(
        ANY,
        "!lobby:example.com",
        "public",
    )


@pytest.mark.asyncio
async def test_configure_managed_room_access_reports_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed Matrix state component must fail the combined application."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", AsyncMock(return_value=False))
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", AsyncMock(return_value=True))

    result = await matrix_rooms._configure_managed_room_access(
        client=AsyncMock(),
        room_key="lobby",
        room_id="!lobby:example.com",
        room_policy=resolve_room_policy(config, "lobby"),
        context="test",
    )

    assert result is False


def test_room_security_helpers_use_only_effective_policy(tmp_path: Path) -> None:
    """Encryption and admins must not consult a second access model."""
    config = membership_config(
        tmp_path,
        agent_rooms=["vault"],
        room_defaults={
            "encrypted": True,
            "admins": ["@admin:example.com", "@partner:example.com"],
        },
    )
    policy = resolve_room_policy(config, "vault")

    assert matrix_rooms._managed_room_should_be_encrypted(policy) is True
    assert matrix_rooms._room_admin_user_ids(policy) == ["@admin:example.com", "@partner:example.com"]


def test_managed_room_initial_state_does_not_embed_admin_ownership_metadata() -> None:
    """Room creation must emit only Matrix power-level schema fields."""
    client = AsyncMock()
    client.user_id = "@router:example.com"

    initial_state = client_room_admin._create_room_initial_state(
        client,
        ["@power:example.com"],
        ["@admin:example.com"],
        encrypted=False,
    )

    power_levels = initial_state[0]["content"]
    assert "io.mindroom.managed_room_admins" not in power_levels
    assert power_levels["users"] == {
        "@power:example.com": 50,
        "@admin:example.com": 100,
        "@router:example.com": 100,
    }


@pytest.mark.asyncio
async def test_managed_room_admin_reconciliation_preserves_existing_admins() -> None:
    """Removing a configured admin must not attempt an equal-power demotion."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "users": {
                "@router:example.com": 100,
                "@manual:example.com": 100,
                "@removed:example.com": 100,
                "@kept:example.com": 50,
            },
            "io.mindroom.managed_room_admins": ["@removed:example.com", "@kept:example.com"],
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!lobby:example.com",
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$power"},
        room_id="!lobby:example.com",
    )

    result = await client_room_admin.ensure_managed_room_power_levels(
        client,
        "!lobby:example.com",
        ["@kept:example.com"],
    )

    assert result is True
    written = client.room_put_state.await_args.kwargs["content"]
    assert written["users"] == {
        "@router:example.com": 100,
        "@manual:example.com": 100,
        "@removed:example.com": 100,
        "@kept:example.com": 100,
    }


@pytest.mark.asyncio
async def test_managed_room_admin_reconciliation_with_empty_policy_preserves_admins() -> None:
    """An empty configured list must leave existing Matrix power levels intact."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "users": {
                "@router:example.com": 100,
                "@removed:example.com": 100,
            },
            "io.mindroom.managed_room_admins": ["@removed:example.com"],
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!lobby:example.com",
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$power"},
        room_id="!lobby:example.com",
    )

    result = await client_room_admin.ensure_managed_room_power_levels(
        client,
        "!lobby:example.com",
        [],
    )

    assert result is True
    written = client.room_put_state.await_args.kwargs["content"]
    assert written["users"] == {
        "@router:example.com": 100,
        "@removed:example.com": 100,
    }


@pytest.mark.asyncio
async def test_existing_room_reconciliation_always_applies_room_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing managed rooms must reconcile membership room state directly."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    policy = resolve_room_policy(config, "lobby")
    apply_access = AsyncMock(return_value=True)
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
    monkeypatch.setattr(matrix_rooms, "ensure_managed_room_power_levels", AsyncMock())
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", apply_access)

    await matrix_rooms._reconcile_joined_existing_room(
        AsyncMock(),
        "lobby",
        "!lobby:example.com",
        config,
        runtime_paths_for(config),
        explicit_room_name=None,
        room_policy=policy,
    )

    apply_access.assert_awaited_once_with(
        client=ANY,
        room_key="lobby",
        room_id="!lobby:example.com",
        room_policy=policy,
        context="existing_room_reconciliation",
        snapshot=None,
    )


@pytest.mark.asyncio
async def test_ensure_all_rooms_passes_resolved_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Room creation must receive one fully resolved policy per managed room."""
    config = membership_config(
        tmp_path,
        agent_rooms=["lobby"],
        room_defaults={"join_policy": "public", "listed": True},
        rooms={"lobby": {"encrypted": True}},
    )
    captured_policies: list[EffectiveRoomPolicy] = []

    async def ensure_room(*, room_policy: EffectiveRoomPolicy, **_kwargs: object) -> str:
        captured_policies.append(room_policy)
        return "!lobby:example.com"

    monkeypatch.setattr(matrix_rooms, "_ensure_room_exists", ensure_room)

    result = await matrix_rooms.ensure_all_rooms_exist(
        AsyncMock(),
        config,
        runtime_paths_for(config),
    )

    assert result == {"lobby": "!lobby:example.com"}
    assert captured_policies == [resolve_room_policy(config, "lobby")]


@pytest.mark.asyncio
async def test_managed_room_reconciliation_has_bounded_overlap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All configured rooms are attempted with at most four requests in flight."""
    rooms = [f"room{i}" for i in range(12)]
    config = membership_config(tmp_path, agent_rooms=rooms)
    active = 0
    peak = 0
    ready = asyncio.Event()
    release = asyncio.Event()

    async def ensure_room(*, room_key: str, **_kwargs: object) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 4:
            ready.set()
        try:
            await release.wait()
            return f"!{room_key}:example.com"
        finally:
            active -= 1

    monkeypatch.setattr(matrix_rooms, "_ensure_room_exists", ensure_room)
    task = asyncio.create_task(matrix_rooms.ensure_all_rooms_exist(AsyncMock(), config, runtime_paths_for(config)))
    try:
        async with asyncio.timeout(1):
            await ready.wait()
        assert active == peak == 4
    finally:
        release.set()
        result = await task
    assert result == {key: f"!{key}:example.com" for key in rooms}
    assert active == 0
    assert peak == 4


@pytest.mark.asyncio
async def test_aliases_of_same_room_do_not_reconcile_concurrently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Alias duplication must not interleave policy updates to one physical room."""
    config = membership_config(tmp_path, agent_rooms=["first", "second"])
    client = AsyncMock()
    client.homeserver = "https://example.com"
    client.rooms = {"!same:example.com": Mock()}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse("#alias:example.com", "!same:example.com", [])
    active = 0
    peak = 0

    async def reconcile(*_args: object, **_kwargs: object) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
        finally:
            active -= 1

    monkeypatch.setattr(matrix_rooms, "_add_room", Mock())
    monkeypatch.setattr(matrix_rooms, "_reconcile_joined_existing_room", reconcile)
    result = await matrix_rooms.ensure_all_rooms_exist(client, config, runtime_paths_for(config))
    client.user_id = "@router:example.com"
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [
            {"type": "m.room.member", "state_key": client.user_id, "content": {"membership": "join"}},
        ],
        "!same:example.com",
    )
    await matrix_rooms.reconcile_managed_rooms(client, config, runtime_paths_for(config), result)
    assert result == {"first": "!same:example.com", "second": "!same:example.com"}
    assert peak == 1
