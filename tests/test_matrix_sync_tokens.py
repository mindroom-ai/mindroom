"""Tests for Matrix sync token persistence."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.sync_certification import SyncCheckpoint, SyncTrustState
from mindroom.matrix.sync_tokens import clear_sync_token, load_sync_token, load_sync_token_record, save_sync_token
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


def _certification_path(tmp_path: Path, *, agent_name: str = "code") -> Path:
    return tmp_path / "sync_tokens" / f"{agent_name}.token.certified"


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
    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "token": "s12345",
        "version": "mindroom-sync-token-v1",
    }
    assert not _certification_path(tmp_path).exists()
    assert load_sync_token(tmp_path, "code") == "s12345"
    token_record = load_sync_token_record(tmp_path, "code")
    assert token_record is not None
    assert token_record.certified is True
    assert token_record.checkpoint == SyncCheckpoint("s12345")


def test_legacy_marker_file_does_not_certify_plaintext_token(tmp_path: Path) -> None:
    """Older marker-only tokens restore for sync continuity but are not certified checkpoints."""
    saved_batch = "s_marker_only"
    token_path = _token_path(tmp_path)
    certification_path = _certification_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(saved_batch, encoding="utf-8")
    certification_path.write_text("legacy-marker\n", encoding="utf-8")

    token_record = load_sync_token_record(tmp_path, "code")

    assert token_record is not None
    assert token_record.token == saved_batch
    assert token_record.certified is False


def test_clear_sync_token_removes_saved_token(tmp_path: Path) -> None:
    """Clearing should remove an existing persisted token."""
    save_sync_token(tmp_path, "code", "s12345")

    clear_sync_token(tmp_path, "code")

    assert load_sync_token(tmp_path, "code") is None
    assert not _token_path(tmp_path).exists()
    assert not _certification_path(tmp_path).exists()


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


@pytest.mark.asyncio
async def test_legacy_plaintext_sync_token_restores_without_cache_trust(tmp_path: Path) -> None:
    """Origin/main plaintext tokens are sync continuity only, not cache-trust roots."""
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

    assert client.next_batch == "s_legacy"
    assert bot._sync_trust_state is SyncTrustState.COLD

    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_after_legacy"
    response.rooms = MagicMock(join={})

    await bot._on_sync_response(response)

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_after_legacy"  # noqa: S105
    assert token_record.certified is True
    assert token_record.checkpoint == SyncCheckpoint("s_after_legacy")


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
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert load_sync_token(tmp_path, bot.agent_name) is None
    assert bot._sync_trust_state is SyncTrustState.UNCERTAIN


@pytest.mark.asyncio
async def test_unknown_pos_restored_first_sync_saves_later_checkpoint(tmp_path: Path) -> None:
    """After M_UNKNOWN_POS, later successful sync responses can save a fresh checkpoint."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected"
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    bot._first_sync_done = True
    response = MagicMock(spec=nio.SyncResponse)
    response.next_batch = "s_later"
    response.rooms = MagicMock(join={})
    await bot._on_sync_response(response)

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_later"  # noqa: S105
    assert token_record.checkpoint == SyncCheckpoint("s_later")


@pytest.mark.asyncio
async def test_unknown_pos_after_first_sync_clears_client_and_saved_token(tmp_path: Path) -> None:
    """Post-start M_UNKNOWN_POS must not leave a poisoned sync token in place."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_rejected_after_start"
    bot._first_sync_done = True
    bot._runtime_view.mark_runtime_started()
    save_sync_token(tmp_path, bot.agent_name, "s_rejected_after_start")
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)

    assert bot.client.next_batch is None
    assert load_sync_token(tmp_path, bot.agent_name) is None
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

    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.token == "s_later_after_unknown_pos"  # noqa: S105
    assert token_record.checkpoint == SyncCheckpoint("s_later_after_unknown_pos")


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
    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.checkpoint == SyncCheckpoint("s_latest")


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_flushes_latest_sync_token(tmp_path: Path) -> None:
    """Shutdown should flush the latest cache-certified sync token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.next_batch = "s_shutdown"
    bot._sync_trust_state = SyncTrustState.CERTIFIED
    bot._sync_checkpoint = SyncCheckpoint("s_shutdown")
    bot._coalescing_gate.drain_all = AsyncMock()

    await bot.prepare_for_sync_shutdown()

    assert load_sync_token(tmp_path, bot.agent_name) == "s_shutdown"
    token_record = load_sync_token_record(tmp_path, bot.agent_name)
    assert token_record is not None
    assert token_record.checkpoint == SyncCheckpoint("s_shutdown")


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_skips_precallback_uncertified_token(tmp_path: Path) -> None:
    """Shutdown must not flush a nio-advanced token before sync-response certification starts."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._coalescing_gate.drain_all = AsyncMock()
    save_sync_token(tmp_path, bot.agent_name, "s_before_precallback")
    bot._runtime_view.mark_runtime_started()
    bot._restore_saved_sync_token()

    bot.client.next_batch = "s_after_precallback"

    await bot.prepare_for_sync_shutdown()

    assert load_sync_token(tmp_path, bot.agent_name) == "s_before_precallback"
