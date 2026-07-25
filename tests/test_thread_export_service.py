"""Tests for thread-export account-group orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest

from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export import ThreadExportTarget, export_threads_once, export_threads_to_targets_once
from mindroom.thread_export.models import ThreadExportGroupFailure, ThreadExportRoom
from mindroom.thread_export.storage import _ROOT_MARKER_FILENAME
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import (
    mark_thread_export_root,
    mock_runtime_support,
    successful_group_result,
    thread_export_config,
    write_invited_rooms,
    write_thread_export_matrix_state,
)


@pytest.mark.asyncio
async def test_export_threads_once_records_group_failure_and_closes_resources(tmp_path: Path) -> None:
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
    """User-created invited rooms should export with the invited entity account."""
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
    assert [call.args[1].agent_name for call in login.await_args_list] == ["general", "general"]
    assert stats.rooms_exported == 2
    assert client.close.await_count == 2


@pytest.mark.asyncio
async def test_export_threads_once_deduplicates_invited_rooms_already_in_state(tmp_path: Path) -> None:
    """A room tracked in matrix_state and an invite store should export only in the state group."""
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
@pytest.mark.parametrize(
    ("include_state_rooms", "room_filter"),
    [
        (False, None),
        (True, "!user-room:localhost"),
    ],
)
async def test_export_threads_once_retracts_discovered_invited_room_when_disabled(
    tmp_path: Path,
    *,
    include_state_rooms: bool,
    room_filter: str | None,
) -> None:
    """Invited-only and filtered passes should retract excluded persisted rooms without login."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(
        tmp_path,
        account_keys=("agent_general",),
        include_rooms=include_state_rooms,
    )
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    output_dir = runtime_paths.storage_root / "thread_exports"
    mark_thread_export_root(output_dir)
    invited_export_dir = output_dir / quote("!user-room:localhost", safe="")
    invited_export_dir.mkdir()
    (invited_export_dir / "index.json").write_text("{}\n", encoding="utf-8")
    (invited_export_dir / f"{quote('$old:localhost', safe='')}.yaml").write_text(
        "version: 1\n",
        encoding="utf-8",
    )

    with (
        patch("mindroom.thread_export.service.login_agent_user", new=AsyncMock()) as login,
        patch("mindroom.thread_export.service.build_owned_runtime_support") as build_support,
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(),
        ) as export_group,
    ):
        stats = await export_threads_once(
            config=config,
            runtime_paths=runtime_paths,
            room_filter=room_filter,
            include_invited_rooms=False,
        )

    login.assert_not_awaited()
    build_support.assert_not_called()
    export_group.assert_not_awaited()
    assert stats.failures == 0
    assert not invited_export_dir.exists()


@pytest.mark.asyncio
async def test_export_threads_once_continues_after_one_account_login_failure(tmp_path: Path) -> None:
    """A broken account group should not prevent a later group from exporting."""
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
        stats = await export_threads_once(
            config=config,
            runtime_paths=runtime_paths,
            room_filter="!user-room:localhost",
        )

    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == ["!user-room:localhost"]
    assert stats.rooms_exported == 1
    assert stats.failures == 0


@pytest.mark.asyncio
async def test_failed_export_groups_do_not_create_runtime_support(tmp_path: Path) -> None:
    """An account-assignment failure should not create an unused cache."""
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


@pytest.mark.asyncio
async def test_full_pass_retains_scoped_exports_when_account_group_cannot_run(tmp_path: Path) -> None:
    """An account-group failure must not let final reconciliation retract data."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    rooms = (
        ThreadExportRoom("lobby", "!lobby:localhost", "#lobby:localhost", "Lobby"),
        ThreadExportRoom("dev", "!dev:localhost", "#dev:localhost", "Dev"),
    )
    output_dir = tmp_path / "exports"
    mark_thread_export_root(output_dir)
    for room in rooms:
        room_dir = output_dir / room.key
        room_dir.mkdir()
        (room_dir / "old.yaml").write_text("secret", encoding="utf-8")

    group_failure = ThreadExportGroupFailure(rooms=rooms, error="No usable Matrix account")
    with patch("mindroom.thread_export.service.build_export_groups", return_value=[group_failure]):
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=(
                ThreadExportTarget(
                    output_dir=output_dir,
                    required_member_user_id="@alice:localhost",
                ),
            ),
        )

    assert stats[0].failures == 2
    assert all("No usable Matrix account" in failure.error for failure in stats[0].failed_items)
    assert all((output_dir / room.key / "old.yaml").read_text(encoding="utf-8") == "secret" for room in rooms)


@pytest.mark.asyncio
async def test_aliased_target_output_directories_are_all_skipped(tmp_path: Path) -> None:
    """A symlinked agent workspace must preserve the corpus both aliases resolve to."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    agents_dir = tmp_path / "agents"
    real_agent_dir = agents_dir / "agent_primary"
    output_dir = real_agent_dir / "workspace" / "thread_exports"
    existing_export = output_dir / "lobby" / "old.yaml"
    existing_export.parent.mkdir(parents=True)
    existing_export.write_text("secret", encoding="utf-8")
    aliased_agent_dir = agents_dir / "agent_alias"
    aliased_agent_dir.symlink_to(real_agent_dir, target_is_directory=True)
    targets = (
        ThreadExportTarget(aliased_agent_dir / "workspace" / "thread_exports"),
        ThreadExportTarget(output_dir),
    )

    with (
        patch("mindroom.thread_export.service.build_export_groups") as build_export_groups,
        patch("mindroom.thread_export.service.logger.warning") as warning,
    ):
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=targets,
        )

    assert existing_export.read_text(encoding="utf-8") == "secret"
    assert tuple(item.output_dir for item in stats) == tuple(target.output_dir for target in targets)
    assert [item.failures for item in stats] == [1, 1]
    assert all(item.failed_items[0].room_key is None for item in stats)
    assert all("overlaps another enabled target" in item.failed_items[0].error for item in stats)
    assert str(targets[1].output_dir) in stats[0].failed_items[0].error
    assert str(targets[0].output_dir) in stats[1].failed_items[0].error
    assert warning.call_count == 2
    build_export_groups.assert_not_called()


@pytest.mark.parametrize("nested_first", [False, True])
@pytest.mark.asyncio
async def test_nested_target_output_directories_are_all_skipped(
    tmp_path: Path,
    *,
    nested_first: bool,
) -> None:
    """Ancestor and descendant targets must both fail before root creation."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    parent_output_dir = tmp_path / "exports"
    nested_output_dir = parent_output_dir / "nested"
    ordered_dirs = (nested_output_dir, parent_output_dir) if nested_first else (parent_output_dir, nested_output_dir)

    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=tuple(ThreadExportTarget(output_dir) for output_dir in ordered_dirs),
    )

    assert tuple(item.output_dir for item in stats) == ordered_dirs
    assert [item.failures for item in stats] == [1, 1]
    assert all("overlaps another enabled target" in item.failed_items[0].error for item in stats)
    assert str(ordered_dirs[1]) in stats[0].failed_items[0].error
    assert str(ordered_dirs[0]) in stats[1].failed_items[0].error
    assert not parent_output_dir.exists()


@pytest.mark.asyncio
async def test_symlink_loop_target_output_directory_fails_closed(tmp_path: Path) -> None:
    """A symlink loop should fail when the root is prepared, not silently resolve."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    first_link = tmp_path / "first"
    second_link = tmp_path / "second"
    first_link.symlink_to(second_link, target_is_directory=True)
    second_link.symlink_to(first_link, target_is_directory=True)
    output_dir = first_link / "thread_exports"

    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=(ThreadExportTarget(output_dir),),
    )

    assert stats[0].output_dir == output_dir
    assert stats[0].failures == 1
    assert stats[0].failed_items[0].room_key is None
    assert "output directory preparation failed" in stats[0].failed_items[0].error


@pytest.mark.asyncio
async def test_symlinked_final_target_is_skipped_without_touching_destination(tmp_path: Path) -> None:
    """A lone symlinked final output directory should fail storage preparation."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    outside = tmp_path / "outside"
    victim = outside / "victim" / "keep.yaml"
    victim.parent.mkdir(parents=True)
    victim.write_text("secret", encoding="utf-8")
    output_dir = tmp_path / "thread_exports"
    output_dir.symlink_to(outside, target_is_directory=True)

    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=(ThreadExportTarget(output_dir),),
    )

    assert stats[0].failures == 1
    assert "symlinked thread export root" in stats[0].failed_items[0].error
    assert victim.read_text(encoding="utf-8") == "secret"
    assert output_dir.is_symlink()


@pytest.mark.parametrize(
    "authored_output_dir",
    [
        pytest.param(Path(), id="current-directory"),
        pytest.param(Path("missing-tail") / "..", id="terminal-parent"),
    ],
)
@pytest.mark.asyncio
async def test_terminal_traversal_output_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authored_output_dir: Path,
) -> None:
    """A terminal traversal component must not promote its parent to the root."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    keep = tmp_path / "unrelated" / "keep.txt"
    keep.parent.mkdir()
    keep.write_text("unrelated", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=(ThreadExportTarget(authored_output_dir),),
    )

    assert stats[0].output_dir == authored_output_dir
    assert stats[0].failures == 1
    assert "must end in an explicit directory name" in stats[0].failed_items[0].error
    assert keep.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.asyncio
async def test_explicit_broad_output_directory_is_rejected(tmp_path: Path) -> None:
    """A shared directory must retain unrelated children and remain unmarked."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    documents_file = tmp_path / "documents" / "keep.txt"
    cache_file = tmp_path / "cache" / "state.db"
    documents_file.parent.mkdir()
    cache_file.parent.mkdir()
    documents_file.write_text("document", encoding="utf-8")
    cache_file.write_text("cache", encoding="utf-8")

    stats = await export_threads_to_targets_once(
        config=config,
        runtime_paths=runtime_paths,
        targets=(ThreadExportTarget(tmp_path),),
    )

    assert stats[0].failures == 1
    assert stats[0].failed_items[0].room_key is None
    assert documents_file.read_text(encoding="utf-8") == "document"
    assert cache_file.read_text(encoding="utf-8") == "cache"
    assert not (tmp_path / _ROOT_MARKER_FILENAME).exists()


@pytest.mark.asyncio
async def test_aliased_targets_are_skipped_while_unique_target_completes(
    tmp_path: Path,
) -> None:
    """Rejected aliases should remain inert while a disjoint target reconciles."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))
    real_agent_dir = tmp_path / "agents" / "agent_primary"
    shared_output_dir = real_agent_dir / "workspace" / "thread_exports"
    shared_export = shared_output_dir / "lobby" / "old.yaml"
    shared_export.parent.mkdir(parents=True)
    shared_export.write_text("secret", encoding="utf-8")
    aliased_agent_dir = tmp_path / "agents" / "agent_alias"
    aliased_agent_dir.symlink_to(real_agent_dir, target_is_directory=True)
    healthy_output_dir = tmp_path / "healthy"
    mark_thread_export_root(healthy_output_dir)
    stale_room = healthy_output_dir / "stale"
    stale_room.mkdir()
    (stale_room / "index.json").write_text("{}\n", encoding="utf-8")
    targets = (
        ThreadExportTarget(aliased_agent_dir / "workspace" / "thread_exports"),
        ThreadExportTarget(shared_output_dir),
        ThreadExportTarget(healthy_output_dir),
    )
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
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=targets,
        )

    assert [item.failures for item in stats] == [1, 1, 0]
    assert stats[2].rooms_exported == 1
    assert export_group.await_args.kwargs["targets"] == (targets[2],)
    assert shared_export.read_text(encoding="utf-8") == "secret"
    assert not stale_room.exists()


@pytest.mark.asyncio
async def test_full_pass_with_zero_exported_rooms_skips_reconciliation(tmp_path: Path) -> None:
    """A full pass with no positive room evidence must preserve its corpus."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    output_dir = tmp_path / "exports"
    mark_thread_export_root(output_dir)
    existing_export = output_dir / "lobby" / "old.yaml"
    existing_export.parent.mkdir()
    existing_export.write_text("secret", encoding="utf-8")

    with (
        patch("mindroom.thread_export.service.build_export_groups", return_value=[]),
        patch("mindroom.thread_export.service.select_export_account"),
        patch("mindroom.thread_export.service.reconcile_room_directories") as reconcile,
        patch("mindroom.thread_export.service.logger.warning") as warning,
    ):
        stats = await export_threads_to_targets_once(
            config=config,
            runtime_paths=runtime_paths,
            targets=(ThreadExportTarget(output_dir),),
        )

    assert stats[0].rooms_exported == 0
    assert existing_export.read_text(encoding="utf-8") == "secret"
    reconcile.assert_not_called()
    warning.assert_called_once_with(
        "Skipping thread export directory reconciliation without exported rooms",
        output_dir=str(output_dir),
        retained_rooms=0,
        failures=0,
    )
