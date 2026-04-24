"""Tests for Matrix sync token persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_token, save_sync_token
from mindroom.matrix.users import AgentMatrixUser
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


def test_load_sync_token_returns_none_when_missing(tmp_path: Path) -> None:
    """First-run agents should have no saved sync token."""
    assert load_sync_token(tmp_path, "code") is None


def test_load_sync_token_returns_none_for_whitespace_only_file(tmp_path: Path) -> None:
    """Whitespace-only token files should be treated as missing."""
    token_path = _token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(" \n\t ", encoding="utf-8")

    assert load_sync_token(tmp_path, "code") is None


def test_save_sync_token_round_trip(tmp_path: Path) -> None:
    """Saving and loading should round-trip the token value."""
    save_sync_token(tmp_path, "code", "s12345")

    token_path = _token_path(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "s12345"
    assert load_sync_token(tmp_path, "code") == "s12345"


def test_clear_sync_token_removes_saved_token(tmp_path: Path) -> None:
    """Clearing should remove an existing persisted token."""
    save_sync_token(tmp_path, "code", "s12345")

    clear_sync_token(tmp_path, "code")

    assert load_sync_token(tmp_path, "code") is None
    assert not _token_path(tmp_path).exists()


def test_clear_sync_token_is_idempotent(tmp_path: Path) -> None:
    """Clearing a missing token should be a no-op."""
    clear_sync_token(tmp_path, "code")

    assert load_sync_token(tmp_path, "code") is None


@pytest.mark.asyncio
async def test_bot_start_restores_saved_sync_token(tmp_path: Path) -> None:
    """Startup should hydrate the nio client from the previously saved token."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_saved")

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


def test_restore_saved_sync_token_ignores_invalid_utf8(tmp_path: Path) -> None:
    """Malformed token bytes should fall back to a cold sync instead of crashing startup."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    bot._restore_saved_sync_token()

    assert bot.client.next_batch is None


@pytest.mark.asyncio
async def test_unknown_pos_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Rejected first-sync saved tokens should be removed before nio retries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started(restored_sync_token=True)
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert load_sync_token(tmp_path, bot.agent_name) is None
    assert bot._runtime_view.restored_sync_token is False
    assert bot._runtime_view.pre_runtime_thread_cache_trusted is False


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_suppresses_later_token_persistence(tmp_path: Path) -> None:
    """A rejected restored token must not be replaced by a later same-runtime token."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started(restored_sync_token=True)
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    bot.client.next_batch = "s_later"
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later"
    response.rooms = MagicMock(join={})
    await bot._on_sync_response(response)

    assert load_sync_token(tmp_path, bot.agent_name) is None


@pytest.mark.asyncio
async def test_unknown_pos_after_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Post-start M_UNKNOWN_POS must not leave a poisoned sync token in place."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_after_start"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started(restored_sync_token=True)
    bot._runtime_view.mark_sync_catchup_applied()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected_after_start")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert load_sync_token(tmp_path, bot.agent_name) is None
    assert bot._runtime_view.restored_sync_token is False
    assert bot._runtime_view.pre_runtime_thread_cache_trusted is False


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

    assert load_sync_token(tmp_path, bot.agent_name) == "s_latest"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    response = MagicMock()
    response.next_batch = "s_shutdown"
    assert bot._certify_sync_response_token(response)
    bot._coalescing_gate.drain_all = AsyncMock()

    await bot.prepare_for_sync_shutdown()

    assert load_sync_token(tmp_path, bot.agent_name) == "s_shutdown"


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_skips_precallback_uncertified_token(tmp_path: Path) -> None:
    """Shutdown must not flush a nio-advanced token before sync-response certification starts."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._coalescing_gate.drain_all = AsyncMock()
    save_sync_token(tmp_path, bot.agent_name, "s_before_precallback")
    restored_sync_token = bot._restore_saved_sync_token()
    bot._runtime_view.mark_runtime_started(restored_sync_token=restored_sync_token)

    bot.client.next_batch = "s_after_precallback"

    await bot.prepare_for_sync_shutdown()

    assert load_sync_token(tmp_path, bot.agent_name) == "s_before_precallback"
