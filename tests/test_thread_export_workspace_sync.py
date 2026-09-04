"""Tests for the native workspace thread-export runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentThreadExportConfig
from mindroom.config.main import Config
from mindroom.matrix.identity import MatrixID
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_export.models import ThreadExportAccumulator, ThreadExportRoom, ThreadExportTarget
from mindroom.thread_export.storage import _ROOT_MARKER_FILENAME, write_thread_payload
from mindroom.thread_export.workspace_sync import (
    _WORKSPACE_EXPORT_DIRNAME,
    ThreadExportBot,
    WorkspaceThreadExportDeps,
    WorkspaceThreadExportRunner,
    enabled_thread_export_agents,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.thread_export_helpers import write_invited_rooms, write_thread_export_matrix_state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.thread_export.models import ThreadExportStats

pytestmark = pytest.mark.asyncio

EXPORT_PATH = "mindroom.thread_export.workspace_sync.export_threads_to_sources"


@dataclass
class _FakeBot:
    """The slice of a running bot the runner reads."""

    user_id: str
    running: bool = True
    client: object | None = field(default_factory=Mock)
    principal: object = field(default_factory=Mock)

    @property
    def matrix_id(self) -> MatrixID:
        return MatrixID.parse(self.user_id)

    def journal_principal(self) -> object:
        return self.principal


def _bots(*bots: _FakeBot) -> dict[str, ThreadExportBot]:
    by_name = {}
    for bot in bots:
        agent_name = bot.matrix_id.username.removeprefix("mindroom_")
        by_name[agent_name] = cast("ThreadExportBot", bot)
    return by_name


def _config(tmp_path: Path, agents: dict[str, AgentConfig]) -> Config:
    return bind_runtime_paths(Config(agents=agents), test_runtime_paths(tmp_path))


def _runner(config: Config, bots: dict[str, ThreadExportBot]) -> WorkspaceThreadExportRunner:
    return WorkspaceThreadExportRunner(
        WorkspaceThreadExportDeps(
            runtime_paths=runtime_paths_for(config),
            config_provider=lambda: config,
            bot_provider=bots.get,
            debounce_seconds=0,
        ),
    )


def _stats_for_targets(**kwargs: object) -> tuple[ThreadExportStats, ...]:
    targets = cast("Sequence[ThreadExportTarget]", kwargs["targets"])
    return tuple(ThreadExportAccumulator(target=target, rooms_exported=1).stats() for target in targets)


def _export_mock() -> AsyncMock:
    return AsyncMock(side_effect=_stats_for_targets)


def _write_owned_export(output_dir: Path) -> Path:
    """Create one marker-backed export tree and return the thread file it holds."""
    room = ThreadExportRoom(key="lobby", room_id="!lobby:localhost", alias="#lobby:localhost", name="Lobby")
    write_thread_payload(
        output_dir,
        room,
        "$thread:localhost",
        {
            "version": 1,
            "room": {"key": room.key, "id": room.room_id, "alias": room.alias, "name": room.name},
            "thread": {"id": "$thread:localhost", "source": "matrix"},
            "messages": [],
        },
    )
    thread_files = list(output_dir.rglob("*.yaml"))
    assert len(thread_files) == 1
    return thread_files[0]


def _materialize_private_instance(config: Config, runtime_paths: RuntimePaths, requester_id: str) -> Path:
    """Create a private instance through the core materialization boundary."""
    return resolve_agent_runtime(
        "secret",
        config,
        runtime_paths,
        ToolExecutionIdentity(
            channel="matrix",
            agent_name="secret",
            requester_id=requester_id,
            room_id="!private:localhost",
            thread_id="thread",
            resolved_thread_id="thread",
            session_id="session",
        ),
        create=True,
    ).state_root


def test_enabled_agents_are_those_with_the_setting(tmp_path: Path) -> None:
    """Enabled agents are those with the setting."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig()),
            "other": AgentConfig(display_name="Other"),
        },
    )
    assert enabled_thread_export_agents(config) == {"code": AgentThreadExportConfig()}


async def test_activity_marks_coalesce_into_one_exact_room_pass(tmp_path: Path) -> None:
    """Activity marks coalesce into one pass per distinct room."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.mark_room_activity("!lobby:localhost")
        runner.mark_room_activity("!lobby:localhost")
        runner.mark_room_activity("!dev:localhost")
        await runner._run_pass_once()

    export.assert_awaited_once()
    call = export.await_args
    assert call is not None
    assert call.kwargs["full_pass"] is False
    assert sorted(room.room_id for source in call.kwargs["sources"] for room in source.rooms) == [
        "!dev:localhost",
        "!lobby:localhost",
    ]


async def test_full_pass_subsumes_dirty_rooms(tmp_path: Path) -> None:
    """Full pass subsumes dirty rooms."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.mark_room_activity("!lobby:localhost")
        runner.queue_full_pass()
        await runner._run_pass_once()

    export.assert_awaited_once()
    call = export.await_args
    assert call is not None
    assert call.kwargs["full_pass"] is True
    assert sorted(room.room_id for source in call.kwargs["sources"] for room in source.rooms) == [
        "!dev:localhost",
        "!lobby:localhost",
    ]


async def test_run_loop_debounces_and_stops(tmp_path: Path) -> None:
    """Run loop debounces and stops."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    exported = asyncio.Event()

    async def _export(**kwargs: object) -> tuple[ThreadExportStats, ...]:
        exported.set()
        return _stats_for_targets(**kwargs)

    with patch(EXPORT_PATH, new=AsyncMock(side_effect=_export)) as export:
        task = asyncio.create_task(runner.run())
        runner.mark_room_activity("!lobby:localhost")
        await asyncio.wait_for(exported.wait(), timeout=5)
        runner.stop()
        await asyncio.wait_for(task, timeout=5)

    export.assert_awaited_once()


async def test_pass_failure_does_not_stop_the_runner(tmp_path: Path) -> None:
    """Pass failure does not stop the runner."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    export = AsyncMock(side_effect=[RuntimeError("boom"), _stats_for_targets])

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()
        runner.queue_full_pass()
        await runner._run_pass_once()

    assert export.await_count == 2


async def test_shared_agent_target_requires_agent_membership(tmp_path: Path) -> None:
    """Shared agent target requires agent membership."""
    config = _config(
        tmp_path,
        {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig(invited_rooms=False))},
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert call.kwargs["targets"] == (
        ThreadExportTarget(
            output_dir=runtime_paths.storage_root / "agents" / "code" / "workspace" / _WORKSPACE_EXPORT_DIRNAME,
            required_member_user_ids=("@mindroom_code:localhost",),
            include_invited_rooms=False,
            trusted_root=runtime_paths.storage_root,
        ),
    )


async def test_invited_rooms_read_through_the_invited_entity_bot(tmp_path: Path) -> None:
    """Invited rooms read through the invited entity bot."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    write_invited_rooms(runtime_paths, "code", ["!private:localhost"])
    router = _FakeBot("@mindroom_router:localhost")
    code = _FakeBot("@mindroom_code:localhost")
    runner = _runner(config, _bots(router, code))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    sources = call.kwargs["sources"]
    assert [(source.client, tuple(room.room_id for room in source.rooms)) for source in sources] == [
        (router.client, ("!lobby:localhost", "!dev:localhost")),
        (code.client, ("!private:localhost",)),
    ]
    assert sources[1].reader.reader.hydrator.self_sender == "@mindroom_code:localhost"
    assert call.kwargs["unreadable_rooms"] == []


async def test_not_running_router_leaves_work_pending(tmp_path: Path) -> None:
    """Not running router leaves work pending."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    router = _FakeBot("@mindroom_router:localhost", running=False)
    runner = _runner(config, _bots(router, _FakeBot("@mindroom_code:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()
        export.assert_not_awaited()
        router.running = True
        await runner._run_pass_once()

    export.assert_awaited_once()
    call = export.await_args
    assert call is not None
    assert call.kwargs["full_pass"] is True


async def test_not_running_invited_entity_is_reported_unreadable(tmp_path: Path) -> None:
    """Not running invited entity is reported unreadable."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    write_invited_rooms(runtime_paths, "code", ["!private:localhost"])
    runner = _runner(
        config,
        _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost", client=None)),
    )
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert len(call.kwargs["sources"]) == 1
    [(rooms, error)] = call.kwargs["unreadable_rooms"]
    assert [room.room_id for room in rooms] == ["!private:localhost"]
    assert error == "Bot 'code' is not running"


async def test_full_pass_clears_exports_of_agents_without_the_setting(tmp_path: Path) -> None:
    """Full pass clears exports of agents without the setting."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig()),
            "other": AgentConfig(display_name="Other"),
        },
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    other_export_dir = runtime_paths.storage_root / "agents" / "other" / "workspace" / _WORKSPACE_EXPORT_DIRNAME
    stale_thread = _write_owned_export(other_export_dir)
    unowned_dir = runtime_paths.storage_root / "agents" / "other" / "workspace" / "notes"
    unowned_dir.mkdir(parents=True)
    (unowned_dir / "keep.yaml").write_text("keep", encoding="utf-8")
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost")))

    with patch(EXPORT_PATH, new=_export_mock()):
        runner.queue_full_pass()
        await runner._run_pass_once()

    assert not stale_thread.exists()
    assert (other_export_dir / _ROOT_MARKER_FILENAME).exists()
    assert (unowned_dir / "keep.yaml").exists()


async def test_agent_without_a_bot_gets_no_target(tmp_path: Path) -> None:
    """Agent without a bot gets no target."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    export.assert_not_awaited()


def _private_config(tmp_path: Path, *, scope: str = "owner_and_agent") -> Config:
    return _config(
        tmp_path,
        {
            "secret": AgentConfig(
                display_name="Secret",
                private=AgentPrivateConfig(per="user"),
                thread_exports=AgentThreadExportConfig.model_validate({"private_room_scope": scope}),
            ),
        },
    )


async def test_private_agent_gets_one_owner_scoped_target_per_validated_instance(tmp_path: Path) -> None:
    """Private agent gets one owner scoped target per validated instance."""
    config = _private_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    alice_root = _materialize_private_instance(config, runtime_paths, "@alice:localhost")
    bob_root = _materialize_private_instance(config, runtime_paths, "@bob:localhost")
    ghost_root = runtime_paths.storage_root / "private_instances" / "ghost-0000000000000000" / "secret"
    ghost_thread = _write_owned_export(ghost_root / "secret_data" / _WORKSPACE_EXPORT_DIRNAME)
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_secret:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    exported = {target.required_member_user_ids: target.output_dir for target in call.kwargs["targets"]}
    assert exported == {
        ("@alice:localhost", "@mindroom_secret:localhost"): alice_root / "secret_data" / _WORKSPACE_EXPORT_DIRNAME,
        ("@bob:localhost", "@mindroom_secret:localhost"): bob_root / "secret_data" / _WORKSPACE_EXPORT_DIRNAME,
    }
    assert not ghost_thread.exists()


async def test_private_owner_scope_requires_only_the_owner(tmp_path: Path) -> None:
    """Private owner scope requires only the owner."""
    config = _private_config(tmp_path, scope="owner")
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    _materialize_private_instance(config, runtime_paths, "@alice:localhost")
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_secret:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert [target.required_member_user_ids for target in call.kwargs["targets"]] == [("@alice:localhost",)]


async def test_symlinked_private_root_is_ignored(tmp_path: Path) -> None:
    """Symlinked private root is ignored."""
    config = _private_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    external_root = tmp_path / "external" / "secret"
    external_thread = _write_owned_export(external_root / "secret_data" / _WORKSPACE_EXPORT_DIRNAME)
    symlink_root = runtime_paths.storage_root / "private_instances" / "untrusted" / "secret"
    symlink_root.parent.mkdir(parents=True)
    symlink_root.symlink_to(external_root, target_is_directory=True)
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_secret:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    export.assert_not_awaited()
    assert external_thread.exists()


async def test_unreadable_private_identity_does_not_block_other_targets(tmp_path: Path) -> None:
    """A private instance whose record cannot be read is skipped, and the pass still runs."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig()),
            "secret": AgentConfig(
                display_name="Secret",
                private=AgentPrivateConfig(per="user"),
                thread_exports=AgentThreadExportConfig(),
            ),
        },
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    alice_root = _materialize_private_instance(config, runtime_paths, "@alice:localhost")
    existing_thread = _write_owned_export(alice_root / "secret_data" / _WORKSPACE_EXPORT_DIRNAME)
    runner = _runner(
        config,
        _bots(
            _FakeBot("@mindroom_router:localhost"),
            _FakeBot("@mindroom_code:localhost"),
            _FakeBot("@mindroom_secret:localhost"),
        ),
    )
    export = _export_mock()

    with (
        patch(
            "mindroom.thread_export.workspace_sync.load_private_instance_identity",
            side_effect=PermissionError("record unreadable"),
        ),
        patch(EXPORT_PATH, new=export),
    ):
        runner.mark_room_activity("!lobby:localhost")
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert [target.required_member_user_ids for target in call.kwargs["targets"]] == [("@mindroom_code:localhost",)]
    assert existing_thread.exists()


async def test_incremental_pass_matches_room_ids_exactly(tmp_path: Path) -> None:
    """A dirty room ID selects that room only, never a room whose ID merely contains it."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    write_invited_rooms(runtime_paths, "code", ["!lobby:localhost2", "!private:localhost"])
    runner = _runner(config, _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost")))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.mark_room_activity("!lobby:localhost")
        runner.mark_room_activity("!private:localhost")
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert [tuple(room.room_id for room in source.rooms) for source in call.kwargs["sources"]] == [
        ("!lobby:localhost",),
        ("!private:localhost",),
    ]
