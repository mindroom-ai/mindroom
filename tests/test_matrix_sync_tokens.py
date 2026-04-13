"""Tests for Matrix sync token persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.sync_tokens import load_sync_token, save_sync_token
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths


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
    return AgentBot(
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


def test_save_sync_token_uses_safe_replace_and_fsyncs_parent_directory(tmp_path: Path) -> None:
    """Saving should use a unique temp file, safe replacement, and a directory fsync."""
    replace_calls: list[tuple[Path, Path]] = []
    original_replace = Path.replace

    def tracked_safe_replace(tmp_path_arg: Path, target_path_arg: Path) -> None:
        replace_calls.append((tmp_path_arg, target_path_arg))
        original_replace(tmp_path_arg, target_path_arg)

    with (
        patch("mindroom.matrix.sync_tokens.constants.safe_replace", side_effect=tracked_safe_replace),
        patch("mindroom.matrix.sync_tokens._fsync_directory") as fsync_directory_mock,
    ):
        save_sync_token(tmp_path, "code", "s12345")

    token_path = _token_path(tmp_path)
    assert token_path.read_text(encoding="utf-8") == "s12345"
    assert load_sync_token(tmp_path, "code") == "s12345"
    assert len(replace_calls) == 1
    tmp_token_path, replaced_token_path = replace_calls[0]
    assert replaced_token_path == token_path
    assert tmp_token_path.parent == token_path.parent
    assert tmp_token_path.name.startswith("code.token.")
    assert tmp_token_path.name.endswith(".tmp")
    assert tmp_token_path.name != "code.token.tmp"
    fsync_directory_mock.assert_called_once_with(token_path.parent)


def test_save_sync_token_keeps_tmp_file_when_replace_fails(tmp_path: Path) -> None:
    """A failed replace should not delete the only fully written token copy."""
    with (
        patch("mindroom.matrix.sync_tokens.constants.safe_replace", side_effect=OSError("busy")),
        pytest.raises(OSError, match="busy"),
    ):
        save_sync_token(tmp_path, "code", "s12345")

    token_path = _token_path(tmp_path)
    tmp_files = list(token_path.parent.glob("code.token.*.tmp"))
    assert not token_path.exists()
    assert len(tmp_files) == 1
    assert tmp_files[0].read_text(encoding="utf-8") == "s12345"


@pytest.mark.asyncio
async def test_bot_start_restores_saved_sync_token(tmp_path: Path) -> None:
    """Startup should hydrate the nio client from the previously saved token."""
    bot = _agent_bot(tmp_path)
    save_sync_token(tmp_path, bot.agent_name, "s_saved")

    client = AsyncMock()
    client.add_event_callback = MagicMock()
    client.add_response_callback = MagicMock()

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
    bot.client = AsyncMock()
    bot.client.next_batch = None

    token_path = _token_path(tmp_path, agent_name=bot.agent_name)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_bytes(b"\xff\xfe\xfd")

    bot._restore_saved_sync_token()

    assert bot.client.next_batch is None


@pytest.mark.asyncio
async def test_on_sync_response_persists_latest_sync_token(tmp_path: Path) -> None:
    """Successful sync responses should update the saved next_batch token."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    response = MagicMock()
    response.next_batch = "s_latest"

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(response)

    assert load_sync_token(tmp_path, bot.agent_name) == "s_latest"


@pytest.mark.asyncio
async def test_sync_token_writes_are_throttled_but_shutdown_flushes_latest_token(tmp_path: Path) -> None:
    """Sync responses should be throttled while shutdown still forces the newest token to disk."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    bot.client.next_batch = None
    bot._coalescing_gate.drain_all = AsyncMock()

    first_response = MagicMock()
    first_response.next_batch = "s_first"
    second_response = MagicMock()
    second_response.next_batch = "s_second"

    with (
        patch("mindroom.bot.save_sync_token") as save_sync_token_mock,
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.bot.time.monotonic", side_effect=[10.0, 10.0, 20.0, 20.0, 25.0]),
        patch.object(bot, "_emit_agent_lifecycle_event", AsyncMock()),
    ):
        await bot._on_sync_response(first_response)
        await bot._on_sync_response(second_response)
        await bot.prepare_for_sync_shutdown()

    assert save_sync_token_mock.call_args_list == [
        call(tmp_path, bot.agent_name, "s_first"),
        call(tmp_path, bot.agent_name, "s_second"),
    ]
