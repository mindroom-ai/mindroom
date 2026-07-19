"""Tests for Matrix sync token persistence."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot, _create_task_wrapper
from mindroom.coalescing import CoalescingDrainResult, CoalescingGate, IngressAdmissionClosedError, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescedBatch, CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.dispatch_handoff import PendingDispatchMetadata
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.matrix.cache.event_cache import EventCacheBackendUnavailableError
from mindroom.matrix.cache.postgres_event_cache import PostgresEventCache
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_checkpoint, save_sync_token
from mindroom.matrix.users import AgentMatrixUser
from mindroom.runtime_shutdown import GENERIC_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.coalescing import LaneSlot, _GateEntry

_CACHE_GENERATION = "test-cache-generation"


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
    bot = AgentBot(
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
    )
    install_runtime_cache_support(bot)
    return bot


def _token_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token"


def _certification_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token.certified"


def _load_sync_token_value(tmp_path: Path, agent_name: str) -> str | None:
    checkpoint = load_sync_checkpoint(tmp_path, agent_name)
    if checkpoint is None:
        return None
    return checkpoint.token


def _text_event(event_id: str, body: str, origin_server_ts: int) -> nio.RoomMessageText:
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def _room_member_event(event_id: str = "$member-join") -> nio.RoomMemberEvent:
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": event_id,
            "sender": "@alice:localhost",
            "state_key": "@alice:localhost",
            "origin_server_ts": 1,
            "content": {"membership": "join"},
            "unsigned": {"prev_content": {"membership": "leave"}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


def _pending(event: nio.RoomMessageText) -> PendingEvent:
    return PendingEvent(
        event=event,
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind="message",
    )


def test_load_sync_token_returns_none_when_missing(tmp_path: Path) -> None:
    """First-run agents should have no saved sync token."""
    assert _load_sync_token_value(tmp_path, "code") is None


def test_load_sync_token_returns_none_for_whitespace_only_file(tmp_path: Path) -> None:
    """Whitespace-only token files should be treated as missing."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" \n\t ", encoding="utf-8")

    assert _load_sync_token_value(tmp_path, "code") is None


def test_save_sync_token_round_trip(tmp_path: Path) -> None:
    """Saving and loading should round-trip the token value."""
    save_sync_token(tmp_path, "code", "s12345", cache_generation=_CACHE_GENERATION)

    token_path = _token_path(tmp_path)
    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "cache_generation": _CACHE_GENERATION,
        "token": "s12345",
        "version": "mindroom-sync-token-v2",
    }
    assert not _certification_path(tmp_path).exists()
    assert _load_sync_token_value(tmp_path, "code") == "s12345"
    checkpoint = load_sync_checkpoint(tmp_path, "code")
    assert checkpoint is not None
    assert checkpoint.token == "s12345"  # noqa: S105
    assert checkpoint.cache_generation == _CACHE_GENERATION


def test_v1_certified_record_is_invalidated_by_principal_owned_cache_schema(tmp_path: Path) -> None:
    """Pre-v11 certified records cannot establish cache trust after the schema reset."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(
        '{"token":"s_old","version":"mindroom-sync-token-v1"}\n',
        encoding="utf-8",
    )

    assert load_sync_checkpoint(tmp_path, "code") is None


def test_legacy_marker_file_does_not_certify_plaintext_token(tmp_path: Path) -> None:
    """Older marker-only tokens cannot restore without a cache generation."""
    saved_batch = "s_marker_only"
    token_path = _token_path(tmp_path)
    certification_path = _certification_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(saved_batch, encoding="utf-8")
    certification_path.write_text("legacy-marker\n", encoding="utf-8")

    token_record = load_sync_checkpoint(tmp_path, "code")

    assert token_record is None


def test_clear_sync_token_removes_saved_token(tmp_path: Path) -> None:
    """Clearing should remove an existing persisted token."""
    save_sync_token(tmp_path, "code", "s12345", cache_generation=_CACHE_GENERATION)

    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None
    assert not _token_path(tmp_path).exists()
    assert not _certification_path(tmp_path).exists()


def test_clear_sync_token_is_idempotent(tmp_path: Path) -> None:
    """Clearing a missing token should be a no-op."""
    clear_sync_token(tmp_path, "code")

    assert _load_sync_token_value(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_bot_start_restores_saved_sync_token(tmp_path: Path) -> None:
    """Startup should hydrate the nio client from the previously saved token."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_saved",
        cache_generation=bot.event_cache.cache_generation,
    )

    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch == "s_saved"


@pytest.mark.asyncio
async def test_leave_cleanup_restart_purges_only_current_sqlite_principal(tmp_path: Path) -> None:
    """A restart after leave cleanup interruption must discard only the departed principal."""
    principal_id = "@mindroom_code:localhost"
    other_principal_id = "@mindroom_other:localhost"
    room_id = "!room:localhost"
    event_id = "$stale"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    principal_cache = root.for_principal(principal_id)
    other_cache = root.for_principal(other_principal_id)
    await principal_cache.store_event(event_id, room_id, event)
    await other_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = principal_cache
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=principal_cache.cache_generation,
    )
    leave_response = MagicMock(spec=nio.SyncResponse)
    leave_response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    interrupted_cleanup = asyncio.CancelledError("process stopped during leave cleanup")
    with (
        patch.object(bot._conversation_cache, "purge_rooms", AsyncMock(side_effect=interrupted_cleanup)),
        pytest.raises(asyncio.CancelledError, match="process stopped"),
    ):
        await bot._apply_own_room_membership_from_sync(leave_response)
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    principal_cache = reopened_root.for_principal(principal_id)
    other_cache = reopened_root.for_principal(other_principal_id)
    bot.event_cache = principal_cache
    client = make_matrix_client_mock(user_id=principal_id)
    client.next_batch = None

    try:
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await bot.start()

        assert await principal_cache.get_event(room_id, event_id) is None
        assert await other_cache.get_event(room_id, event_id) == event
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_login_identity_change_rebinds_principal_cache_view(tmp_path: Path) -> None:
    """Authenticated identity replacement must not retain the old principal's cache view."""
    old_principal_id = "@mindroom_code:localhost"
    new_principal_id = "@mindroom_code:new.example"
    room_id = "!room:localhost"
    event_id = "$old-principal"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "old principal", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    old_cache = root.for_principal(old_principal_id)
    await old_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = old_cache
    matrix_id_before_login = bot.matrix_id

    try:
        bot.agent_user.user_id = new_principal_id
        bot._rebuild_runtime_components_after_login_if_identity_changed(matrix_id_before_login)

        assert bot.event_cache.principal_id == new_principal_id
        assert await bot.event_cache.get_event(room_id, event_id) is None
        assert await old_cache.get_event(room_id, event_id) == event
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_authoritative_leave_clears_checkpoint_before_cache_cleanup(tmp_path: Path) -> None:
    """A crash during leave cleanup must force principal cleanup on the next startup."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=bot.event_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})

    await bot._apply_own_room_membership_from_sync(response)

    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None


@pytest.mark.asyncio
async def test_leave_fence_rejects_delayed_write_before_new_checkpoint(tmp_path: Path) -> None:
    """Certification after leave must not preserve a delayed callback's recreated rows."""
    principal_id = "@mindroom_code:localhost"
    room_id = "!left:localhost"
    event_id = "$event"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    cache = root.for_principal(principal_id)
    await cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = cache
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    try:
        await bot._apply_own_room_membership_from_sync(response)
        await cache.store_event("$late", room_id, {**event, "event_id": "$late"})
        bot._save_sync_checkpoint(SyncCheckpoint("s_after_leave"))

        assert await cache.get_event(room_id, "$late") is None
        assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_after_leave"
    finally:
        await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    try:
        reopened_cache = reopened_root.for_principal(principal_id)
        assert await reopened_cache.get_event(room_id, event_id) is None
        assert await reopened_cache.get_event(room_id, "$late") is None
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_cleanup_failure",
    [
        RuntimeError("call cleanup interrupted"),
        asyncio.CancelledError("call cleanup interrupted"),
    ],
)
async def test_leave_purges_before_failing_call_reconciliation(
    tmp_path: Path,
    call_cleanup_failure: BaseException,
) -> None:
    """Call cleanup cannot suspend or fail before authoritative cache cleanup."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=bot.event_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={"!left:localhost": MagicMock()})
    operation_order: list[str] = []

    async def purge_rooms(_room_ids: object) -> None:
        operation_order.append("purge")

    async def fail_call_cleanup(**_kwargs: object) -> None:
        operation_order.append("call")
        raise call_cleanup_failure

    bot._call_manager = MagicMock()
    bot._call_manager.on_sync_room_membership = AsyncMock(side_effect=fail_call_cleanup)

    with (
        patch.object(bot._conversation_cache, "purge_rooms", side_effect=purge_rooms),
        pytest.raises(type(call_cleanup_failure), match="call cleanup interrupted"),
    ):
        await bot._apply_own_room_membership_from_sync(response)

    assert operation_order == ["purge", "call"]
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_checkpoint_clear_failure_defers_durable_leave_cleanup_for_replay(tmp_path: Path) -> None:
    """A failed checkpoint unlink must preserve old durable rows and poison this runtime."""
    principal_id = "@mindroom_code:localhost"
    room_id = "!left:localhost"
    event_id = "$stale"
    event = {
        "event_id": event_id,
        "sender": "@user:localhost",
        "origin_server_ts": 1,
        "type": "m.room.message",
        "content": {"body": "stale", "msgtype": "m.text"},
    }
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    principal_cache = root.for_principal(principal_id)
    await principal_cache.store_event(event_id, room_id, event)
    bot = _agent_bot(tmp_path)
    bot.event_cache = principal_cache
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_leave")
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_leave",
        cache_generation=principal_cache.cache_generation,
    )
    response = MagicMock(spec=nio.SyncResponse)
    response.rooms = MagicMock(join={}, leave={room_id: MagicMock()})
    clear_failure = OSError("checkpoint directory unavailable")

    with patch("mindroom.bot.clear_sync_token", side_effect=clear_failure):
        await bot._apply_own_room_membership_from_sync(response)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_leave"
    await root.close()

    reopened_root = SqliteEventCache(tmp_path / "event-cache.db")
    await reopened_root.initialize()
    reopened_cache = reopened_root.for_principal(principal_id)
    bot.event_cache = reopened_cache
    bot._runtime_view.callback_failure_count = 0
    bot.client = make_matrix_client_mock(user_id=principal_id)
    bot.client.next_batch = None
    try:
        await bot._prepare_cache_and_restore_saved_sync_token()

        assert bot.client.next_batch == "s_before_leave"
        assert await reopened_cache.get_event(room_id, event_id) == event

        await bot._apply_own_room_membership_from_sync(response)

        assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
        assert await reopened_cache.get_event(room_id, event_id) is None
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_bot_start_initializes_postgres_principal_before_restoring_checkpoint(
    tmp_path: Path,
    postgres_event_cache_url: str,
) -> None:
    """A matching principal namespace generation must preserve restart continuity."""
    namespace = f"sync_restore_{uuid.uuid4().hex}"
    principal_id = "@mindroom_code:localhost"
    seed_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    seed_view = seed_root.for_principal(principal_id)
    await seed_view.initialize()
    generation = seed_view.cache_generation
    assert generation is not None
    await seed_root.close()

    bot = _agent_bot(tmp_path)
    reopened_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    bot.event_cache = reopened_root.for_principal(principal_id)
    assert bot.event_cache.cache_generation is None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_postgres_restart",
        cache_generation=generation,
    )
    client = make_matrix_client_mock(user_id=principal_id)
    client.next_batch = None

    try:
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await bot.start()

        assert client.next_batch == "s_postgres_restart"
        assert bot.event_cache.cache_generation == generation
    finally:
        await reopened_root.close()


@pytest.mark.asyncio
async def test_postgres_outage_preserves_unverified_checkpoint_until_generation_recovers(
    tmp_path: Path,
    postgres_event_cache_url: str,
) -> None:
    """A transient outage must leave a valid cache/checkpoint pair available for a later restart."""
    namespace = f"sync_restore_outage_{uuid.uuid4().hex}"
    principal_id = "@mindroom_code:localhost"
    room_id = "!room:localhost"
    event_id = "$cached-before-outage"
    event = {
        "content": {"body": "cached", "msgtype": "m.text"},
        "event_id": event_id,
        "origin_server_ts": 1,
        "room_id": room_id,
        "sender": "@user:localhost",
        "type": "m.room.message",
    }
    seed_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    seed_view = seed_root.for_principal(principal_id)
    await seed_view.initialize()
    await seed_view.store_event(event_id, room_id, event)
    generation = seed_view.cache_generation
    assert generation is not None
    await seed_root.close()
    save_sync_token(
        tmp_path,
        "code",
        "s_before_outage",
        cache_generation=generation,
    )

    unavailable_bot = _agent_bot(tmp_path)
    unavailable_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    unavailable_bot.event_cache = unavailable_root.for_principal(principal_id)
    unavailable_client = make_matrix_client_mock(user_id=principal_id)
    unavailable_client.next_batch = None
    empty_response = MagicMock(spec=nio.SyncResponse)
    empty_response.next_batch = "s_empty_during_outage"
    empty_response.rooms = MagicMock(join={}, leave={})
    message_event = nio.RoomMessageText.from_dict(event)
    event_response = MagicMock(spec=nio.SyncResponse)
    event_response.next_batch = "s_event_during_outage"
    event_response.rooms = MagicMock(
        join={room_id: MagicMock(timeline=MagicMock(events=[message_event], limited=False))},
        leave={},
    )
    try:
        with (
            patch.object(unavailable_bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=unavailable_client)),
            patch.object(unavailable_bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(unavailable_bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
            patch(
                "mindroom.matrix.cache.postgres_event_cache._initialize_postgres_event_cache_db",
                AsyncMock(side_effect=EventCacheBackendUnavailableError("database unavailable")),
            ),
        ):
            await unavailable_bot.start()
            await unavailable_bot._on_sync_response(empty_response)
            await unavailable_bot._on_sync_response(event_response)

        assert unavailable_client.next_batch is None
        assert _load_sync_token_value(tmp_path, unavailable_bot.agent_name) == "s_before_outage"
    finally:
        await unavailable_root.close()

    recovered_bot = _agent_bot(tmp_path)
    recovered_root = PostgresEventCache(database_url=postgres_event_cache_url, namespace=namespace)
    recovered_view = recovered_root.for_principal(principal_id)
    recovered_bot.event_cache = recovered_view
    recovered_client = make_matrix_client_mock(user_id=principal_id)
    recovered_client.next_batch = None
    try:
        with (
            patch.object(recovered_bot, "ensure_user_account", AsyncMock()),
            patch("mindroom.bot.login_agent_user", AsyncMock(return_value=recovered_client)),
            patch.object(recovered_bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(recovered_bot, "_set_presence_with_model_info", AsyncMock()),
            patch("mindroom.bot.interactive.init_persistence"),
        ):
            await recovered_bot.start()

        assert recovered_client.next_batch == "s_before_outage"
        assert await recovered_view.get_event(room_id, event_id) == event
    finally:
        await recovered_root.close()


@pytest.mark.asyncio
async def test_sqlite_checkpoint_generation_rejects_matrix_principal_rebind(tmp_path: Path) -> None:
    """A retained agent token must not cross a Matrix account or homeserver change."""
    root = SqliteEventCache(tmp_path / "event-cache.db")
    await root.initialize()
    old_principal = root.for_principal("@mindroom_code:old.example")
    new_principal = root.for_principal("@mindroom_code:new.example")
    old_generation = old_principal.cache_generation
    assert old_generation is not None
    assert new_principal.cache_generation != old_generation
    save_sync_token(
        tmp_path,
        "code",
        "s_old_principal",
        cache_generation=old_generation,
    )
    bot = _agent_bot(tmp_path)
    bot.event_cache = new_principal
    bot.client = make_matrix_client_mock(user_id=new_principal.principal_id)
    bot.client.next_batch = None

    try:
        await bot._prepare_cache_and_restore_saved_sync_token()

        assert bot.client.next_batch is None
        assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    finally:
        await root.close()


@pytest.mark.asyncio
async def test_bot_start_rejects_checkpoint_from_reset_cache_generation(tmp_path: Path) -> None:
    """A certified token cannot skip history after its backing cache was reset."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        cache_generation="stale-cache-generation",
    )
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_bot_start_preserves_checkpoint_when_cache_generation_is_unavailable(tmp_path: Path) -> None:
    """An unavailable generation starts cold without destroying an unverified checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.event_cache.cache_generation = None
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_stale",
        cache_generation="old-cache-generation",
    )
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_stale"


@pytest.mark.asyncio
async def test_bot_start_purges_untrusted_cache_without_checkpoint_when_generation_is_unavailable(
    tmp_path: Path,
) -> None:
    """Generation failure cannot excuse stale rows when no checkpoint proves their sync position."""
    bot = _agent_bot(tmp_path)
    bot.event_cache.cache_generation = None
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    bot.event_cache.purge_principal.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_plaintext_sync_token_starts_cold(tmp_path: Path) -> None:
    """Plaintext tokens cannot restore continuity without cache-generation proof."""
    bot = _agent_bot(tmp_path)
    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("s_legacy", encoding="utf-8")

    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.next_batch = None

    with (
        patch.object(bot, "ensure_user_account", AsyncMock()),
        patch("mindroom.bot.login_agent_user", AsyncMock(return_value=client)),
        patch.object(bot, "_set_avatar_if_available", AsyncMock()),
        patch.object(bot, "_set_presence_with_model_info", AsyncMock()),
        patch("mindroom.bot.interactive.init_persistence"),
    ):
        await bot.start()

    assert client.next_batch is None
    assert bot._sync_trust_state is SyncTrustState.COLD

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_legacy"
    response.rooms = MagicMock(join={})

    await bot._on_sync_response(response)

    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_after_legacy"  # noqa: S105


def test_restore_saved_sync_token_ignores_invalid_utf8(tmp_path: Path) -> None:
    """Malformed token bytes should fall back to a cold sync instead of crashing startup."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    bot._restore_loaded_sync_token(bot._loaded_sync_token_for_certification().checkpoint)

    assert bot.client.next_batch is None


@pytest.mark.asyncio
async def test_unknown_pos_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Rejected first-sync saved tokens should be removed before nio retries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", cache_generation=_CACHE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_saves_later_checkpoint(tmp_path: Path) -> None:
    """After M_UNKNOWN_POS, later successful sync responses can save a fresh checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected", cache_generation=_CACHE_GENERATION)
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later"
    response.rooms = MagicMock(join={})
    await bot._on_sync_response(response)

    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_later"  # noqa: S105


@pytest.mark.asyncio
async def test_unknown_pos_after_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Post-start M_UNKNOWN_POS must not leave a poisoned sync token in place."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_after_start"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_rejected_after_start",
        cache_generation=_CACHE_GENERATION,
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_non_restored_runtime_allows_later_checkpoint(tmp_path: Path) -> None:
    """M_UNKNOWN_POS should fail closed, then allow later certified tokens."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_cold"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot.client.next_batch = "s_later_after_unknown_pos"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later_after_unknown_pos"
    response.rooms = MagicMock(join={"!room:localhost": MagicMock(timeline=MagicMock(events=[], limited=False))})
    await bot._on_sync_response(response)

    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_later_after_unknown_pos"  # noqa: S105


@pytest.mark.asyncio
async def test_on_sync_response_persists_latest_sync_token(tmp_path: Path) -> None:
    """Successful sync responses should update the saved next_batch token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_latest"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_latest"
    response.rooms = MagicMock(join={})

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_latest"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_latest"  # noqa: S105


@pytest.mark.asyncio
async def test_sync_response_side_effect_failure_clears_certified_checkpoint(tmp_path: Path) -> None:
    """A post-certification sync side effect failure must poison the saved token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_after_side_effect_failure"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_side_effect_failure"
    response.rooms = MagicMock(join={})
    bot._emit_agent_lifecycle_event = AsyncMock(side_effect=RuntimeError("bot ready failed"))  # type: ignore[method-assign]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="bot ready failed"),
    ):
        await bot._on_sync_response(response)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_shutdown"
    checkpoint = load_sync_checkpoint(tmp_path, bot.agent_name)
    assert checkpoint is not None
    assert checkpoint.token == "s_shutdown"  # noqa: S105


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_cancelled_ingress(tmp_path: Path) -> None:
    """Incomplete bounded drains must not save certified shutdown checkpoints."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        return_value=CoalescingDrainResult(completed=False, cancelled_unready_count=1),
    )

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_unsettled_callbacks(tmp_path: Path) -> None:
    """Shutdown must not checkpoint if callback tasks timed out before the gate drain."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=False)):
        await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_post_drain_background_work(tmp_path: Path) -> None:
    """Shutdown must prove owner background work is settled after the gate drain too."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    wait_for_background_tasks = AsyncMock(side_effect=[True, False])

    with patch("mindroom.bot.wait_for_background_tasks", new=wait_for_background_tasks):
        await bot.prepare_for_sync_shutdown()

    assert wait_for_background_tasks.await_count == 2
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_callback_failure_prevents_certified_shutdown_checkpoint(tmp_path: Path) -> None:
    """A Matrix callback exception must make the certified sync token unsafe."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_after_bad_callback")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))

    async def failing_callback() -> None:
        msg = "canonical key lookup failed"
        raise RuntimeError(msg)

    callback = _create_task_wrapper(failing_callback, owner=bot._runtime_view)
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_callback_failure_clears_saved_checkpoint_immediately(tmp_path: Path) -> None:
    """A failed Matrix callback must clear already-persisted sync continuity."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_before_failure", cache_generation=_CACHE_GENERATION)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_failure")

    async def failing_callback() -> None:
        msg = "callback failed"
        raise RuntimeError(msg)

    callback = _create_task_wrapper(
        failing_callback,
        owner=bot._runtime_view,
        on_error=bot._mark_callback_failed,
    )
    await callback()
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


def test_callback_failure_blocks_later_certified_checkpoint(tmp_path: Path) -> None:
    """No later sync response may restore certification after a callback failure."""
    bot = _agent_bot(tmp_path)
    bot._mark_callback_failed()

    bot._apply_sync_certification_decision(
        SyncCertificationDecision(
            state=SyncTrustState.CERTIFIED,
            checkpoint_to_save=SyncCheckpoint("s_after_failure"),
        ),
    )

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_room_member_callback_failure_prevents_certified_checkpoint(tmp_path: Path) -> None:
    """Room-member callback exceptions must use the same sync-failure accounting."""
    bot = _agent_bot(tmp_path)
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_member_failure",
        cache_generation=_CACHE_GENERATION,
    )
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_before_member_failure")
    bot._on_room_member = AsyncMock(side_effect=RuntimeError("member callback failed"))  # type: ignore[method-assign]
    wrapper = bot._create_room_member_task_wrapper()
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)

    await wrapper(room, _room_member_event())
    await wait_for_background_tasks(timeout=0.5, owner=bot._runtime_view)

    assert bot._runtime_view.callback_failure_count == 1
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_incomplete_shutdown_drain_poison_persists_across_repeated_shutdown(tmp_path: Path) -> None:
    """A later no-op shutdown call must not save a checkpoint after unsafe drain work."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_previous", cache_generation=_CACHE_GENERATION)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(
        side_effect=[
            CoalescingDrainResult(completed=False, cancelled_unready_count=1),
            CoalescingDrainResult(completed=True),
        ],
    )

    await bot.prepare_for_sync_shutdown()
    await bot.prepare_for_sync_shutdown()

    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_skips_precallback_uncertified_token(tmp_path: Path) -> None:
    """Shutdown must not flush a nio-advanced token before sync-response certification starts."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    save_sync_token(
        tmp_path,
        bot.agent_name,
        "s_before_precallback",
        cache_generation=bot.event_cache.cache_generation,
    )
    bot._runtime_view.mark_runtime_started()
    bot._restore_loaded_sync_token(bot._loaded_sync_token_for_certification().checkpoint)

    bot.client.next_batch = "s_after_precallback"

    await bot.prepare_for_sync_shutdown()

    assert _load_sync_token_value(tmp_path, bot.agent_name) == "s_before_precallback"


@pytest.mark.asyncio
async def test_receive_time_gate_shutdown_drains_unresolved_admission() -> None:
    """Sync shutdown should wait for an admitted prompt to become ready and dispatch it."""
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    event = cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": "$waiting",
                "sender": "@user:localhost",
                "origin_server_ts": 1000,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "waiting"},
            },
        ),
    )
    key = CoalescingKey(room.room_id, "$thread", "@user:localhost")
    release_ready = asyncio.Event()
    dispatched: list[list[str]] = []

    async def dispatch_batch(batch: object) -> None:
        dispatched.append(list(batch.source_event_ids))

    async def ready_event() -> object:
        await release_ready.wait()
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=event, room=room, source_kind="message"),
        )

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: True,
    )

    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$waiting",
        source_kind="message",
        ready_task=asyncio.create_task(ready_event()),
    )
    shutdown_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0)

    assert shutdown_task.done() is False

    release_ready.set()
    await shutdown_task

    assert dispatched == [["$waiting"]]


@pytest.mark.asyncio
async def test_receive_time_gate_shutdown_does_not_poison_later_generation() -> None:
    """A shutdown drain should not prevent a later clean sync generation from admitting prompts."""
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!room:localhost"
    key = CoalescingKey(room.room_id, "$thread", "@user:localhost")
    dispatched: list[list[str]] = []

    def text_event(event_id: str, body: str) -> nio.RoomMessageText:
        return cast(
            "nio.RoomMessageText",
            nio.RoomMessageText.from_dict(
                {
                    "event_id": event_id,
                    "sender": "@user:localhost",
                    "origin_server_ts": 1000,
                    "room_id": room.room_id,
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": body},
                },
            ),
        )

    async def dispatch_batch(batch: object) -> None:
        dispatched.append(list(batch.source_event_ids))

    shutting_down = True
    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        is_shutting_down=lambda: shutting_down,
    )

    waiting_release = asyncio.Event()

    async def waiting_ready() -> object:
        await waiting_release.wait()
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=text_event("$waiting", "waiting"), room=room, source_kind="message"),
        )

    waiting_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        waiting_slot,
        key=key,
        source_event_id="$waiting",
        source_kind="message",
        ready_task=asyncio.create_task(waiting_ready()),
    )
    drain_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0)
    waiting_release.set()
    await drain_task

    shutting_down = False

    async def next_ready() -> object:
        return ReadyPendingEvent(
            pending_event=PendingEvent(event=text_event("$next", "next"), room=room, source_kind="message"),
        )

    next_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        next_slot,
        key=key,
        source_event_id="$next",
        source_kind="message",
        ready_task=asyncio.create_task(next_ready()),
    )
    await gate.drain_all()

    assert dispatched == [["$waiting"], ["$next"]]


@pytest.mark.asyncio
async def test_shutdown_drain_cancels_stuck_ready_task_without_cancelling_dispatch() -> None:
    """Bounded drains should cancel unresolved ready work and report an unsafe result."""
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(stuck_ready()),
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_shutdown_drain_counts_self_cancelled_ready_task_as_incomplete() -> None:
    """Undelivered ready work that cancelled itself still means ingress was not dispatched."""

    async def cancelled_ready() -> ReadyPendingEvent | None:
        raise asyncio.CancelledError

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    unresolved_front_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    ready_task = asyncio.create_task(cancelled_ready())
    await asyncio.gather(ready_task, return_exceptions=True)
    assert ready_task.cancelled()
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=ready_task,
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert unresolved_front_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 2
    assert result.cancelled_unready_count == 1


@pytest.mark.asyncio
async def test_shutdown_drain_releases_stuck_pre_admission_lane_slot() -> None:
    """Bounded drains should release unresolved lane slots and reject late admission."""
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert slot.released is True
    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            source_event_id="$late:localhost",
            source_kind="message",
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
        )


@pytest.mark.asyncio
async def test_shutdown_ready_timeout_closes_ready_result_returned_during_cancellation() -> None:
    """Ready results produced while handling timeout cancellation should be closed once."""
    close_count = 0
    cancelled = asyncio.Event()

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$voice:localhost", "voice", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReadyPendingEvent(pending_event=pending_event)

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(ready()),
    )

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert cancelled.is_set()
    assert close_count == 1
    assert result.completed is False
    assert result.cancelled_unready_count == 1
    assert result.dropped_ready_count == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_ready_wait() -> None:
    """Bounded shutdown should interrupt an already-running shielded ready wait."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck_ready() -> ReadyPendingEvent | None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(stuck_ready()),
    )
    await started.wait()

    result = await gate.drain_all(ready_timeout_seconds=0.01)

    assert result.completed is False
    assert result.released_reservation_count == 1
    assert result.cancelled_unready_count == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_ready_task_self_cancellation_finishes_no_ready() -> None:
    """Ready tasks that cancel themselves should finish as no-ready work."""

    async def cancelled_ready() -> ReadyPendingEvent | None:
        raise asyncio.CancelledError

    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)
    gate.submit_lane_slot(
        slot,
        key=key,
        source_event_id="$voice",
        source_kind=VOICE_SOURCE_KIND,
        ready_task=asyncio.create_task(cancelled_ready()),
    )

    await gate.drain_all()

    assert slot.settled.is_set()
    assert batches == []


@pytest.mark.asyncio
async def test_enter_lane_during_active_bounded_shutdown_returns_released_counted_slot() -> None:
    """New lane slots during bounded shutdown should be pre-released and counted."""
    shutting_down = False

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: shutting_down,
    )
    old_slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")
    shutting_down = True
    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.05))
    await asyncio.sleep(0)

    slot = gate.enter_lane(room_id="!room:localhost", sender_id="@user:localhost")

    assert slot.closed is True
    assert slot.released is True
    assert slot.settled.is_set()

    with pytest.raises(IngressAdmissionClosedError):
        gate.submit_lane_slot(
            slot,
            key=CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            source_event_id="$late:localhost",
            source_kind="message",
            ready_result=ReadyPendingEvent(
                pending_event=_pending(_text_event("$late:localhost", "late", 1000)),
            ),
        )

    result = await drain_task

    assert old_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 2


@pytest.mark.asyncio
async def test_shutdown_timeout_reaches_already_running_same_window_lane_slot_wait() -> None:
    """Bounded shutdown should interrupt same-window lane-slot waits already in progress."""
    shutting_down = False
    wait_entered = asyncio.Event()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.01,
        is_shutting_down=lambda: shutting_down,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    target_slot = gate.enter_lane(room_id=key.room_id, sender_id=key.requester_user_id)

    original_wait_for_lane_slots = gate._wait_for_lane_slots

    async def spy_wait_for_lane_slots(
        wait_gate: _GateEntry,
        slots: list[LaneSlot],
    ) -> None:
        if target_slot in slots:
            wait_entered.set()
        await original_wait_for_lane_slots(wait_gate, slots)

    gate._wait_for_lane_slots = spy_wait_for_lane_slots

    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
        source_event_id="$text:localhost",
        source_kind="message",
    )
    await asyncio.wait_for(wait_entered.wait(), timeout=5.0)

    shutting_down = True
    result = await gate.drain_all(ready_timeout_seconds=0.05)

    assert target_slot.released is True
    assert result.completed is False
    assert result.released_reservation_count == 1


@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_failure_marks_drain_incomplete() -> None:
    """In-flight dispatch failures during bounded shutdown should make the result unsafe."""
    dispatch_entered = asyncio.Event()
    fail_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await fail_dispatch.wait()
        message = "dispatch failed"
        raise RuntimeError(message)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
    )
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch failure")
    fail_dispatch.set()
    result = await drain_task

    assert result.completed is False
    assert result.dispatch_failure_count == 1


@pytest.mark.asyncio
async def test_shutdown_in_flight_dispatch_cancellation_marks_drain_incomplete() -> None:
    """In-flight dispatch cancellation during bounded shutdown should make the result unsafe."""
    dispatch_entered = asyncio.Event()
    dispatch_raised_self_cancel = asyncio.Event()
    cancel_dispatch = asyncio.Event()

    async def dispatch_batch(_batch: CoalescedBatch) -> None:
        dispatch_entered.set()
        await cancel_dispatch.wait()
        dispatch_raised_self_cancel.set()
        raise asyncio.CancelledError

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        is_shutting_down=lambda: True,
    )
    key = CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost")
    await gate.admit(
        key,
        ready_result=ReadyPendingEvent(pending_event=_pending(_text_event("$text:localhost", "typed", 1000))),
    )
    await dispatch_entered.wait()

    drain_task = asyncio.create_task(gate.drain_all(ready_timeout_seconds=0.01))
    for _ in range(100):
        if gate._active_drain_context is not None and gate._gates[key].drain_context is gate._active_drain_context:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("drain context was not installed before dispatch cancellation")
    cancel_dispatch.set()
    result = await drain_task

    assert dispatch_raised_self_cancel.is_set()
    assert result.completed is False
    assert result.dispatch_cancelled_count == 1


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_save_checkpoint_for_undrained_inbox_responses(tmp_path: Path) -> None:
    """A stuck detached inbox response must block the certified shutdown checkpoint."""
    bot = _agent_bot(tmp_path)
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=False)

    await bot.prepare_for_sync_shutdown()

    bot._response_runner.drain_inbox_responses.assert_awaited_once_with(
        cancel_after_seconds=5.0,
        shutdown_intent=GENERIC_SHUTDOWN,
    )
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN
    assert bot._sync_checkpoint is None
    assert _load_sync_token_value(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_passes_cancel_source_to_inbox_drain(tmp_path: Path) -> None:
    """Sync-restart shutdown should preserve provenance for detached inbox responses."""
    bot = _agent_bot(tmp_path)
    bot._coalescing_gate.drain_all = AsyncMock(return_value=CoalescingDrainResult(completed=True))
    bot._response_runner.drain_inbox_responses = AsyncMock(return_value=True)

    await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    bot._response_runner.drain_inbox_responses.assert_awaited_once_with(
        cancel_after_seconds=5.0,
        shutdown_intent=SYNC_RESTART_SHUTDOWN,
    )
