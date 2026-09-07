"""Tests for thread-export account-group orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

import pytest

from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY
from mindroom.thread_export import (
    ThreadExportSource,
    ThreadExportTarget,
    export_threads_once,
    export_threads_to_sources,
    export_threads_to_targets_once,
)
from mindroom.thread_export import service as thread_export_service
from mindroom.thread_export.models import ThreadExportGroup, ThreadExportRoom
from mindroom.thread_export.projected_history import export_conversation_reader
from mindroom.thread_export.storage import _ROOT_MARKER_FILENAME, _ROOT_MARKER_TEXT
from tests.conftest import runtime_paths_for
from tests.thread_export_helpers import (
    mark_thread_export_root,
    successful_group_result,
    thread_export_config,
    write_invited_rooms,
    write_thread_export_matrix_state,
)


def _client_for_group(_group: ThreadExportGroup) -> Mock:
    return Mock()


def _source_for_group(group: ThreadExportGroup) -> ThreadExportSource:
    return ThreadExportSource(client=_client_for_group(group), reader=Mock(), rooms=group.rooms)


@pytest.mark.asyncio
async def test_export_threads_once_records_group_failure_without_closing_borrowed_resources(tmp_path: Path) -> None:
    """An unexpected group failure should keep borrowed resources open and return room failures."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    client = Mock()
    client.close = AsyncMock()

    with (
        patch("tests.test_thread_export_service._client_for_group", new=Mock(return_value=client)),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=RuntimeError("export failed")),
        ),
    ):
        stats = await export_threads_once(source_provider=_source_for_group, config=config, runtime_paths=runtime_paths)

    client.close.assert_not_awaited()
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
        patch("tests.test_thread_export_service._client_for_group", new=Mock(return_value=client)) as login,
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_once(source_provider=_source_for_group, config=config, runtime_paths=runtime_paths)

    group_room_ids = [[room.room_id for room in call.kwargs["rooms"]] for call in export_group.await_args_list]
    assert group_room_ids == [
        ["!lobby:localhost", "!dev:localhost"],
        ["!user-room:localhost"],
    ]
    assert [call.args[0].entity_name for call in login.call_args_list] == ["router", "general"]
    assert stats.rooms_exported == 2
    client.close.assert_not_awaited()


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
        patch("tests.test_thread_export_service._client_for_group", new=Mock(return_value=client)),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        await export_threads_once(source_provider=_source_for_group, config=config, runtime_paths=runtime_paths)

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
    """Invited-only and filtered passes should retract excluded persisted rooms without acquiring a source."""
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
        patch("tests.test_thread_export_service._client_for_group", new=Mock()) as login,
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(),
        ) as export_group,
    ):
        stats = await export_threads_once(
            source_provider=_source_for_group,
            config=config,
            runtime_paths=runtime_paths,
            room_filter=room_filter,
            include_invited_rooms=False,
        )

    login.assert_not_called()
    export_group.assert_not_awaited()
    assert stats.failures == 0
    assert not invited_export_dir.exists()


@pytest.mark.asyncio
async def test_export_threads_once_continues_after_one_owner_is_unavailable(tmp_path: Path) -> None:
    """A broken account group should not prevent a later group from exporting."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY, "agent_general"))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])
    client = Mock()
    client.close = AsyncMock()
    login = Mock(side_effect=[RuntimeError("owner unavailable"), client])

    with (
        patch("tests.test_thread_export_service._client_for_group", new=login),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_to_targets_once(
            source_provider=_source_for_group,
            config=config,
            runtime_paths=runtime_paths,
            targets=(ThreadExportTarget(output_dir=tmp_path / "exports"),),
        )

    assert login.call_count == 2
    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == ["!user-room:localhost"]
    assert stats[0].rooms_exported == 1
    assert stats[0].failures == 2
    assert all("owner unavailable" in failure.error for failure in stats[0].failed_items)


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
        patch("tests.test_thread_export_service._client_for_group", new=Mock(return_value=client)),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_once(
            source_provider=_source_for_group,
            config=config,
            runtime_paths=runtime_paths,
            room_filter="!user-room:localhost",
        )

    export_group.assert_awaited_once()
    assert [room.room_id for room in export_group.await_args.kwargs["rooms"]] == ["!user-room:localhost"]
    assert stats.rooms_exported == 1
    assert stats.failures == 0


@pytest.mark.asyncio
async def test_an_unavailable_owner_fails_only_the_targets_that_wanted_it(tmp_path: Path) -> None:
    """An unavailable room owner fails the targets that requested it and no others."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path, account_keys=(INTERNAL_USER_ACCOUNT_KEY,))
    write_invited_rooms(runtime_paths, "general", ["!user-room:localhost"])

    stats = await export_threads_to_targets_once(
        source_provider=Mock(side_effect=RuntimeError("owner unavailable")),
        config=config,
        runtime_paths=runtime_paths,
        targets=(
            ThreadExportTarget(output_dir=tmp_path / "invited", include_invited_rooms=True),
            ThreadExportTarget(output_dir=tmp_path / "configured", include_invited_rooms=False),
        ),
        room_filter="!user-room:localhost",
    )

    assert stats[0].failures == 1
    assert stats[0].failed_items[0].room_id == "!user-room:localhost"
    assert "owner unavailable" in stats[0].failed_items[0].error
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

    group_failure = ThreadExportGroup(rooms=rooms, entity_name="router")
    with (
        patch("mindroom.thread_export.service.build_export_groups", return_value=[group_failure]),
        patch(
            "tests.test_thread_export_service._client_for_group",
            side_effect=RuntimeError("No usable Matrix account"),
        ),
    ):
        stats = await export_threads_to_targets_once(
            source_provider=_source_for_group,
            config=config,
            runtime_paths=runtime_paths,
            targets=(
                ThreadExportTarget(
                    output_dir=output_dir,
                    required_member_user_ids=("@alice:localhost",),
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
            source_provider=_source_for_group,
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
        source_provider=_source_for_group,
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
        source_provider=_source_for_group,
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
        source_provider=_source_for_group,
        config=config,
        runtime_paths=runtime_paths,
        targets=(ThreadExportTarget(output_dir),),
    )

    assert stats[0].failures == 1
    assert "symlinked thread export root" in stats[0].failed_items[0].error
    assert victim.read_text(encoding="utf-8") == "secret"
    assert output_dir.is_symlink()


@pytest.mark.asyncio
async def test_trusted_root_target_rejects_parent_replaced_after_validation(
    tmp_path: Path,
) -> None:
    """Replacing a validated parent cannot redirect an anchored export target."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    instance_root = tmp_path / "private_instances" / "scope" / "agent"
    output_dir = instance_root / "workspace" / "thread_exports"
    instance_root.mkdir(parents=True)
    saved_instance_root = instance_root.with_name("agent-saved")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    original_prepare = thread_export_service.prepare_export_root
    swapped = False

    def swap_before_prepare(path: Path, *, trusted_root: Path | None = None) -> None:
        nonlocal swapped
        instance_root.rename(saved_instance_root)
        instance_root.symlink_to(outside, target_is_directory=True)
        swapped = True
        original_prepare(path, trusted_root=trusted_root)

    with patch(
        "mindroom.thread_export.service.prepare_export_root",
        side_effect=swap_before_prepare,
    ):
        stats = await export_threads_to_targets_once(
            source_provider=_source_for_group,
            config=config,
            runtime_paths=runtime_paths,
            targets=(
                ThreadExportTarget(
                    output_dir,
                    trusted_root=runtime_paths.storage_root,
                ),
            ),
        )

    assert swapped is True
    assert stats[0].failures == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (outside / "workspace" / "thread_exports").exists()


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
        source_provider=_source_for_group,
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
        source_provider=_source_for_group,
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
        patch("tests.test_thread_export_service._client_for_group", new=Mock(return_value=client)),
        patch(
            "mindroom.thread_export.service.export_threads_for_targets_for_client",
            new=AsyncMock(side_effect=successful_group_result),
        ) as export_group,
    ):
        stats = await export_threads_to_targets_once(
            source_provider=_source_for_group,
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
        patch("mindroom.thread_export.service.reconcile_room_directories") as reconcile,
        patch("mindroom.thread_export.service.logger.warning") as warning,
    ):
        stats = await export_threads_to_targets_once(
            source_provider=_source_for_group,
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


@pytest.mark.asyncio
async def test_export_threads_to_sources_exports_each_source_and_reconciles(tmp_path: Path) -> None:
    """The in-process path validates targets, records unreadable rooms, and reconciles a full pass."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    output_dir = tmp_path / "out"
    lobby = ThreadExportRoom(key="lobby", room_id="!lobby:localhost", alias="", name="Lobby")
    dev = ThreadExportRoom(key="dev", room_id="!dev:localhost", alias="", name="Dev")
    source = ThreadExportSource(client=Mock(), reader=Mock(), rooms=(lobby,))

    with patch(
        "mindroom.thread_export.service.export_threads_for_targets_for_client",
        new=AsyncMock(side_effect=successful_group_result),
    ) as export_source:
        stats = await export_threads_to_sources(
            config=config,
            runtime_paths=runtime_paths,
            sources=(source,),
            targets=(ThreadExportTarget(output_dir=output_dir),),
            unreadable_rooms=(((dev,), "Bot 'code' is not running"),),
            full_pass=True,
        )

    export_source.assert_awaited_once()
    assert export_source.await_args is not None
    assert export_source.await_args.kwargs["client"] is source.client
    assert export_source.await_args.kwargs["rooms"] == (lobby,)
    assert stats[0].rooms_exported == 1
    assert [failure.error for failure in stats[0].failed_items] == ["Bot 'code' is not running"]
    assert (output_dir / _ROOT_MARKER_FILENAME).read_text(encoding="utf-8") == _ROOT_MARKER_TEXT


@pytest.mark.asyncio
async def test_export_threads_to_sources_honors_each_sources_targets(tmp_path: Path) -> None:
    """Each live source exports only to the targets explicitly bound to it."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    room = ThreadExportRoom(key="lobby", room_id="!lobby:localhost", alias="", name="Lobby")
    code_target = ThreadExportTarget(output_dir=tmp_path / "code")
    other_target = ThreadExportTarget(output_dir=tmp_path / "other")
    code_source = ThreadExportSource(
        client=Mock(),
        reader=Mock(),
        rooms=(room,),
        target_output_dirs=(code_target.output_dir,),
    )
    other_source = ThreadExportSource(
        client=Mock(),
        reader=Mock(),
        rooms=(room,),
        target_output_dirs=(other_target.output_dir,),
    )

    with patch(
        "mindroom.thread_export.service.export_threads_for_targets_for_client",
        new=AsyncMock(side_effect=successful_group_result),
    ) as export_source:
        await export_threads_to_sources(
            config=config,
            runtime_paths=runtime_paths,
            sources=(code_source, other_source),
            targets=(code_target, other_target),
            full_pass=False,
        )

    assert export_source.await_count == 2
    assert [call.kwargs["targets"] for call in export_source.await_args_list] == [
        (code_target,),
        (other_target,),
    ]


@pytest.mark.asyncio
async def test_export_threads_to_sources_skips_matrix_work_without_valid_targets(tmp_path: Path) -> None:
    """A target that fails validation is reported and never triggers a source read."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    target = ThreadExportTarget(output_dir=tmp_path / "trailing" / "..")
    source = ThreadExportSource(
        client=Mock(),
        reader=Mock(),
        rooms=(),
        target_output_dirs=(target.output_dir,),
    )

    with patch(
        "mindroom.thread_export.service.export_threads_for_targets_for_client",
        new=AsyncMock(side_effect=successful_group_result),
    ) as export_source:
        stats = await export_threads_to_sources(
            config=config,
            runtime_paths=runtime_paths,
            sources=(source,),
            targets=(target,),
            full_pass=False,
        )

    export_source.assert_not_awaited()
    assert stats[0].failures == 1


@pytest.mark.asyncio
async def test_cancelled_export_drains_its_shielded_hydration(tmp_path: Path) -> None:
    """Cancelling an export cannot leave its private history walk using borrowed resources."""
    config = thread_export_config(tmp_path)
    client = Mock(close=AsyncMock())
    reader = export_conversation_reader(client=client, config=config, store=Mock(), self_sender="@router:localhost")
    source = ThreadExportSource(client=client, reader=reader, rooms=(ThreadExportRoom("lobby", "!lobby", "", ""),))
    started = asyncio.Event()
    finished = asyncio.Event()

    async def hydrate() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    async def exporting(**_kwargs: object) -> None:
        hydrator = reader.reader.hydrator
        await hydrator._shared(hydrator._in_flight, ("!lobby", None), hydrate, name="test_export_hydration")

    with patch("mindroom.thread_export.service.export_threads_for_targets_for_client", side_effect=exporting):
        task = asyncio.create_task(
            export_threads_to_sources(
                config=config,
                runtime_paths=runtime_paths_for(config),
                sources=(source,),
                targets=(ThreadExportTarget(tmp_path / "out"),),
                full_pass=True,
            ),
        )
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            assert finished.is_set()
            assert not reader.reader.hydrator._in_flight
            client.close.assert_not_awaited()
        finally:
            tasks = tuple(reader.reader.hydrator._in_flight.values())
            for child in tasks:
                child.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
