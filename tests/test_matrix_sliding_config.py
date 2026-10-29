"""Sliding settings and room changes reach the owned durable session."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, create_autospec
from uuid import uuid4

import nio
import pytest
from nio.durable import DurableSync, DurableSyncConfig, SlidingSyncConfig, open_durable_sync
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.matrix.state import MatrixState
from mindroom.matrix.sync_loop import bot_ingestion_config, subscribe_sliding_rooms
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import install_bots
from tests.threading_helpers import ThreadingBehaviorTestBase

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["classic", "sliding"])
async def test_subscriptions_add_each_joined_room_whose_state_nio_still_awaits(mode: str) -> None:
    """A joined room outside the list stays subscribed until its state arrives; a sendable or left room adds none."""
    config = Config.model_validate({"matrix_sync": {"mode": mode, "sliding_timeline_limit": 3}})
    session = create_autospec(DurableSync, instance=True)
    awaited, sendable, left = "!awaited:example.org", "!sendable:example.org", "!left:example.org"
    session.client = SimpleNamespace(rooms=dict.fromkeys((awaited, sendable, left)))

    async def wait_for_room_ready(room_id: str) -> bool:
        if room_id == awaited:
            await asyncio.Event().wait()
        return room_id == sendable

    session.wait_for_room_ready.side_effect = wait_for_room_ready

    await subscribe_sliding_rooms(session, config, ["!configured:example.org", "#alias:example.org"])

    if mode == "classic":
        session.update_sliding_subscriptions.assert_not_awaited()
        return
    subscriptions = session.update_sliding_subscriptions.await_args.args[0]
    assert set(subscriptions) == {"!configured:example.org", awaited}
    assert subscriptions[awaited]["timeline_limit"] == 3


def test_sliding_config_retains_timeline_limit_and_classic_default() -> None:
    """Existing Sliding YAML remains valid while Classic remains the default."""
    configured = Config.model_validate({"matrix_sync": {"mode": "sliding", "sliding_timeline_limit": 23}})
    assert configured.matrix_sync.mode == "sliding"
    assert configured.matrix_sync.sliding_timeline_limit == 23
    assert MatrixSyncConfig().mode == "classic"
    assert MatrixSyncConfig().sliding_timeline_limit == 100
    with pytest.raises(ValidationError, match="sliding_timeline_limit"):
        MatrixSyncConfig.model_validate({"sliding_timeline_limit": 0})


def test_sliding_config_preserves_discovery_explicit_rooms_and_extensions() -> None:
    """Rooms outside discovery retain explicit windows and crypto extensions."""
    config = Config()
    config.matrix_sync = MatrixSyncConfig.model_construct(mode="sliding", sliding_timeline_limit=23)
    ingestion = bot_ingestion_config(
        config,
        agent_name="general",
        room_ids=["!outside:example.org", "#unresolved:example.org", "lobby", "!outside:example.org"],
        timeout_ms=5000,
        sync_filter={"room": {"timeline": {"limit": 50}}},
    )
    assert ingestion.sync_timeout_ms == 5000
    sliding = ingestion.sliding
    assert sliding is not None
    assert sliding.conn_id == "mindroom-general"
    room_config = {
        "timeline_limit": 23,
        "required_state": [
            ["m.room.create", ""],
            ["m.room.name", ""],
            ["m.room.topic", ""],
            ["m.room.avatar", ""],
            ["m.room.encryption", ""],
            ["m.room.member", "$LAZY"],
        ],
    }
    assert sliding.lists == {"mindroom": {"ranges": [[0, 99]], **room_config}}
    assert sliding.room_subscriptions == {"!outside:example.org": room_config}
    assert sliding.extensions == {
        "to_device": {"enabled": True},
        "e2ee": {"enabled": True},
        "account_data": {"enabled": True},
    }
    other = bot_ingestion_config(
        config,
        agent_name="router",
        room_ids=[],
        timeout_ms=5000,
        sync_filter={},
    )
    assert other.sliding is not None
    assert other.sliding.conn_id == "mindroom-router"


class TestSlidingBot(ThreadingBehaviorTestBase):
    """The bot updates its existing session after room resolution and joins."""

    @pytest.mark.asyncio
    async def test_owned_login_receives_agent_connection_and_resolved_rooms(
        self,
        bot: AgentBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Opening before deferred joins must still use this bot's current rooms."""
        bot.config.matrix_sync = MatrixSyncConfig.model_construct(mode="sliding", sliding_timeline_limit=17)
        bot.rooms = ["!outside:example.org"]
        client = nio.AsyncClient("https://example.org", bot.agent_user.user_id, device_id="DEVICE")
        client.restore_login(bot.agent_user.user_id, "DEVICE", "token")

        async def login_owned(*_args: object, **kwargs: object) -> SimpleNamespace:
            config = kwargs["config"]
            assert isinstance(config, DurableSyncConfig)
            session = open_durable_sync(
                client,
                consumer_id=uuid4(),
                store_path=tmp_path / "sliding",
                config=config,
            )
            return SimpleNamespace(client=client, session=session)

        monkeypatch.setattr("mindroom.bot.login_agent_owned_session", login_owned)
        await bot._open_owned_matrix_client()
        session = bot._ingestion_session
        assert session is not None
        try:
            assert session._sliding is not None
            request = json.loads(session._sliding.request()[2])
            assert request["conn_id"] == "mindroom-general"
            assert request["room_subscriptions"]["!outside:example.org"]["timeline_limit"] == 17
        finally:
            await session.close()
            await client.close()
            bot._ingestion_session = None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["classic", "sliding"])
    async def test_ensure_rooms_refreshes_subscriptions_after_membership_changes(
        self,
        bot: AgentBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        """Deferred joins and later room edits replace future subscription input."""
        bot.config.matrix_sync = MatrixSyncConfig.model_construct(mode=mode, sliding_timeline_limit=19)
        client = nio.AsyncClient("https://example.org", bot.agent_user.user_id, device_id="DEVICE")
        client.restore_login(bot.agent_user.user_id, "DEVICE", "token")
        session = open_durable_sync(
            client,
            consumer_id=uuid4(),
            store_path=tmp_path / "subscriptions",
            config=DurableSyncConfig(
                sliding=SlidingSyncConfig(room_subscriptions={"!old:example.org": {}}) if mode == "sliding" else None,
            ),
        )
        bot._ingestion_session = session
        joined = False
        left = False

        async def join_rooms() -> None:
            nonlocal joined
            bot.rooms = ["!outside:example.org", "#unresolved:example.org"]
            joined = True

        async def leave_rooms() -> None:
            nonlocal left
            assert joined
            left = True

        monkeypatch.setattr(bot, "join_configured_rooms", join_rooms)
        monkeypatch.setattr(bot, "leave_unconfigured_rooms", leave_rooms)
        try:
            await bot.ensure_rooms()
            assert left
            if mode == "sliding":
                assert session._sliding is not None
                request = json.loads(session._sliding.request()[2])
                assert set(request["room_subscriptions"]) == {"!outside:example.org"}
                assert request["room_subscriptions"]["!outside:example.org"]["timeline_limit"] == 19
                assert ["m.room.member", "$LAZY"] in request["room_subscriptions"]["!outside:example.org"][
                    "required_state"
                ]
                monkeypatch.setattr(bot, "join_configured_rooms", AsyncMock())
                bot.rooms = []
                await bot.ensure_rooms()
                assert json.loads(session._sliding.request()[2])["room_subscriptions"] == {}
            else:
                assert session.config.sliding is None
        finally:
            await session.close()
            await client.close()
            bot._ingestion_session = None

    @pytest.mark.asyncio
    async def test_runtime_room_edit_resolves_alias_and_refreshes_same_session(
        self,
        bot: AgentBot,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hot reload reaches rooms beyond discovery without replacing the source."""
        bot.config.matrix_sync = MatrixSyncConfig.model_construct(mode="sliding", sliding_timeline_limit=29)
        bot.config.agents["general"].rooms = ["outside"]
        state = MatrixState.load(runtime_paths=bot.runtime_paths)
        state.add_room("outside", "!outside:example.org", "#outside:example.org", "Outside")
        state.save(runtime_paths=bot.runtime_paths)
        bot.rooms = ["!old:example.org"]
        bot.running = True
        orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
        orchestrator.config = bot.config
        install_bots(orchestrator, {"general": bot})
        client = nio.AsyncClient("https://example.org", bot.agent_user.user_id, device_id="DEVICE")
        client.restore_login(bot.agent_user.user_id, "DEVICE", "token")
        session = open_durable_sync(
            client,
            consumer_id=uuid4(),
            store_path=tmp_path / "runtime-sliding",
            config=DurableSyncConfig(sliding=SlidingSyncConfig(room_subscriptions={"!old:example.org": {}})),
        )
        bot._ingestion_session = session
        monkeypatch.setattr(bot, "join_configured_rooms", AsyncMock())
        monkeypatch.setattr(bot, "leave_unconfigured_rooms", AsyncMock())
        for method in ("ensure_rooms_exist", "link_root_space", "_ensure_room_invitations"):
            monkeypatch.setattr(orchestrator._rooms, method, AsyncMock())
        monkeypatch.setattr(orchestrator, "refresh_agent_reply_memberships", AsyncMock())
        plan = ConfigUpdatePlan(
            new_config=bot.config,
            restart=set(),
            start=set(),
            remove=set(),
            mindroom_user_changed=False,
            room_access_changed=False,
            matrix_space_changed=False,
            authorization_changed=False,
            reconcile_rooms={"general"},
        )
        try:
            await orchestrator._reconcile_post_update_rooms(plan, changed_entities=set())
            assert bot._ingestion_session is session
            assert bot.rooms == ["!outside:example.org"]
            assert session._sliding is not None
            subscriptions = json.loads(session._sliding.request()[2])["room_subscriptions"]
            assert set(subscriptions) == {"!outside:example.org"}
            assert subscriptions["!outside:example.org"]["timeline_limit"] == 29
        finally:
            await session.close()
            await client.close()
            bot._ingestion_session = None
