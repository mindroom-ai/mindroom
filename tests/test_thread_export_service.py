"""Tests for thread-export account-group orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest

from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export import ThreadExportTarget, export_threads_once, export_threads_to_targets_once
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import (
    mock_runtime_support,
    successful_group_result,
    thread_export_config,
    write_invited_rooms,
    write_thread_export_matrix_state,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_export_threads_once_records_group_failure_and_continues_cleanup(tmp_path: Path) -> None:
    """An unexpected group failure should close resources and return room failures."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.selection.select_export_account", return_value=Mock()),
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()) as close_support,
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=RuntimeError("export failed")),
        ),
    ):
        stats = await export_threads_once(config=config, runtime_paths=runtime_paths)

    client.close.assert_awaited_once()
    close_support.assert_awaited_once()
    assert stats.failures == 2
    assert all("Export group failed: export failed" in failure.error for failure in stats.failed_items)


@pytest.mark.asyncio
async def test_export_threads_once_exports_invited_rooms_with_entity_account(tmp_path: Path) -> None:
    """User-created invited rooms should export in a second group using the invited agent's account."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=("agent_general",))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)) as login,
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_once(config=config, runtime_paths=runtime_paths)

    group_room_ids = [[room.room_id for room in call.kwargs["rooms"]] for call in export_group.await_args_list]
    assert group_room_ids == [
        ["!lobby:localhost", "!dev:localhost"],
        ["!user-room:localhost"],
    ]
    login_agent_names = [call.args[1].agent_name for call in login.await_args_list]
    assert login_agent_names == ["general", "general"]
    invited_room = export_group.await_args_list[1].kwargs["rooms"][0]
    assert invited_room.key == "!user-room:localhost"
    assert stats.rooms_exported == 2
    assert client.close.await_count == 2


@pytest.mark.asyncio
async def test_export_threads_once_continues_after_one_account_login_failure(tmp_path: Path) -> None:
    """A broken account group should not prevent later invited-room groups from exporting."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY, "agent_general"))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    client = Mock()
    client.close = AsyncMock()
    login = AsyncMock(side_effect=[RuntimeError("expired token"), client])

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=login),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=(ThreadExportTarget(output_dir=tmp_path / "exports"),),
        )

    assert login.await_count == 2
    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == ["!user-room:localhost"]
    assert stats[0].rooms_exported == 1
    assert stats[0].failures == 2
    assert all("Matrix login failed: expired token" in failure.error for failure in stats[0].failed_items)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_threads_once_dedups_invited_rooms_already_in_state(tmp_path: Path) -> None:
    """Invited rooms already tracked in matrix_state should not export twice."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=("agent_general",))
    write_invited_rooms(runtime_paths, "general", ["!lobby:localhost"])
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        await export_threads_once(config=config, runtime_paths=runtime_paths)

    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == [
        "!lobby:localhost",
        "!dev:localhost",
    ]


@pytest.mark.asyncio
async def test_export_threads_once_skips_invited_rooms_when_disabled(tmp_path: Path) -> None:
    """include_invited_rooms=False should export only matrix_state rooms."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=("agent_general",))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    invited_export_dir = runtime_paths.storage_root / "thread_exports" / quote("!user-room:localhost", safe="")
    invited_export_dir.mkdir(parents=True)
    (invited_export_dir / "old.yaml").write_text("secret", encoding="utf-8")
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        await export_threads_once(config=config, runtime_paths=runtime_paths, include_invited_rooms=False)

    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == [
        "!lobby:localhost",
        "!dev:localhost",
    ]
    assert not invited_export_dir.exists()


@pytest.mark.asyncio
async def test_export_threads_once_room_filter_selects_invited_room(tmp_path: Path) -> None:
    """A room-id filter matching only an invited room should export just that room."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=("agent_general",))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        await export_threads_once(
            config=config,
            runtime_paths=runtime_paths,
            room_filter="!user-room:localhost",
        )

    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == ["!user-room:localhost"]


@pytest.mark.asyncio
async def test_export_threads_once_records_failure_for_invited_room_without_account(tmp_path: Path) -> None:
    """Invited rooms of an entity without a persisted account should surface as failures."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock(return_value=client)),
        patch("mindroom.thread_export.service.build_owned_runtime_support", return_value=mock_runtime_support()),
        patch("mindroom.thread_export.service.close_owned_runtime_support", new=AsyncMock()),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_once(config=config, runtime_paths=runtime_paths)

    export_group.assert_awaited_once()
    assert stats.failures == 1
    assert stats.failed_items[0].room_id == "!user-room:localhost"
    assert "general" in stats.failed_items[0].error


@pytest.mark.asyncio
async def test_failed_export_groups_do_not_create_runtime_support(tmp_path: Path) -> None:
    """An account-assignment failure should not create a cache with no ready group to use it."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])

    with patch("mindroom.thread_export.service.build_owned_runtime_support") as build_support:
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=(
                ThreadExportTarget(output_dir=tmp_path / "invited", include_invited_rooms=True),
                ThreadExportTarget(output_dir=tmp_path / "configured", include_invited_rooms=False),
            ),
            room_filter="!user-room:localhost",
        )

    build_support.assert_not_called()
    assert stats[0].failures == 1
    assert stats[0].failed_items[0].room_id == "!user-room:localhost"
    assert stats[1].failures == 0
