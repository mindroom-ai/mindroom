"""Tests for the native workspace thread-export runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentThreadExportConfig
from mindroom.config.main import Config
from mindroom.event_journal import EventJournalStore
from mindroom.matrix.identity import MatrixID
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.thread_export.models import ThreadExportAccumulator, ThreadExportRoom, ThreadExportTarget
from mindroom.thread_export.storage import _ROOT_MARKER_FILENAME, write_thread_payload
from mindroom.thread_export.workspace_sync import (
    _WORKSPACE_EXPORT_DIRNAME,
    WorkspaceThreadExportDeps,
    WorkspaceThreadExportRunner,
    _ThreadExportBot,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.thread_export_helpers import write_thread_export_matrix_state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.thread_export.models import ThreadExportSource, ThreadExportStats

pytestmark = pytest.mark.asyncio

EXPORT_PATH = "mindroom.thread_export.workspace_sync.export_threads_to_sources"


@dataclass
class _FakeBot:
    """The slice of a running bot the runner reads."""

    user_id: str
    running: bool = True
    client: object | None = field(default_factory=Mock)
    principal: object = field(default_factory=Mock)
    rooms: list[str] = field(default_factory=lambda: ["!lobby:localhost", "!dev:localhost"])
    invited_room_ids: frozenset[str] = frozenset()
    joined_room_ids: frozenset[str] | None = None
    joined_rooms_error: Exception | None = None
    joined_room_lookup_count: int = 0

    @property
    def matrix_id(self) -> MatrixID:
        return MatrixID.parse(self.user_id)

    @property
    def approval_room_ids(self) -> frozenset[str]:
        return frozenset(self.rooms) | self.invited_room_ids

    def journal_principal(self) -> object:
        return self.principal

    async def current_joined_room_ids(self) -> frozenset[str] | None:
        self.joined_room_lookup_count += 1
        if self.joined_rooms_error is not None:
            raise self.joined_rooms_error
        return self.approval_room_ids if self.joined_room_ids is None else self.joined_room_ids


def _bots(*bots: _FakeBot) -> dict[str, _ThreadExportBot]:
    by_name = {}
    for bot in bots:
        agent_name = bot.matrix_id.username.removeprefix("mindroom_")
        by_name[agent_name] = cast("_ThreadExportBot", bot)
    return by_name


def _config(tmp_path: Path, agents: dict[str, AgentConfig]) -> Config:
    return bind_runtime_paths(Config(agents=agents), test_runtime_paths(tmp_path))


def _runner(config: Config, bots: dict[str, _ThreadExportBot]) -> WorkspaceThreadExportRunner:
    return WorkspaceThreadExportRunner(
        WorkspaceThreadExportDeps(
            runtime_paths=runtime_paths_for(config),
            config_provider=lambda: config,
            bot_provider=bots.get,
            response_admission_gate=ResponseAdmissionGate(),
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


async def test_partial_pass_queries_only_agents_related_to_dirty_rooms(tmp_path: Path) -> None:
    """A partial pass avoids joined-room lookups for unrelated agents."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(
                display_name="Code",
                rooms=["lobby"],
                thread_exports=AgentThreadExportConfig(),
            ),
            "other": AgentConfig(
                display_name="Other",
                rooms=["dev"],
                thread_exports=AgentThreadExportConfig(),
            ),
        },
    )
    write_thread_export_matrix_state(tmp_path)
    code = _FakeBot("@mindroom_code:localhost", rooms=["!lobby:localhost"])
    other = _FakeBot("@mindroom_other:localhost", rooms=["!dev:localhost"])
    runner = _runner(config, _bots(code, other))

    with patch(EXPORT_PATH, new=_export_mock()):
        runner.mark_room_activity("!lobby:localhost")
        await runner._run_pass_once()

    assert code.joined_room_lookup_count == 1
    assert other.joined_room_lookup_count == 0


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


async def test_started_runner_exports_on_activity_and_stops_cleanly(tmp_path: Path) -> None:
    """Started runner exports on activity and stops cleanly."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    exported = asyncio.Event()

    async def _export(**kwargs: object) -> tuple[ThreadExportStats, ...]:
        exported.set()
        return _stats_for_targets(**kwargs)

    with patch(EXPORT_PATH, new=AsyncMock(side_effect=_export)) as export:
        runner.start()
        runner.start()
        runner.mark_room_activity("!lobby:localhost")
        await asyncio.wait_for(exported.wait(), timeout=5)
        task = runner._task
        assert task is not None
        await asyncio.wait_for(runner.stop(), timeout=5)

    export.assert_awaited_once()
    assert task.cancelled()
    assert runner._task is None


async def test_pass_failure_keeps_the_work_for_the_next_trigger(tmp_path: Path) -> None:
    """A crashed pass neither stops the runner nor drops what it was asked to export."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    bots = _bots(_FakeBot("@mindroom_router:localhost"), _FakeBot("@mindroom_code:localhost"))
    runner = _runner(config, bots)
    export = AsyncMock(side_effect=[RuntimeError("boom"), _stats_for_targets])

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        runner.mark_room_activity("!lobby:localhost")
        await runner._run_pass_once()
        await runner._run_pass_once()

    assert export.await_count == 2
    second = export.await_args_list[1]
    assert second.kwargs["full_pass"] is True


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


async def test_each_agent_source_is_bound_only_to_its_own_workspace(tmp_path: Path) -> None:
    """Each workspace uses its agent's principal, including explicit rooms absent from managed state."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(
                display_name="Code",
                rooms=["lobby"],
                thread_exports=AgentThreadExportConfig(),
            ),
            "other": AgentConfig(
                display_name="Other",
                rooms=["!external:localhost"],
                thread_exports=AgentThreadExportConfig(),
            ),
        },
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    code = _FakeBot("@mindroom_code:localhost", rooms=["!lobby:localhost", "!lobby:localhost"])
    other = _FakeBot("@mindroom_other:localhost", rooms=["!external:localhost"])
    runner = _runner(config, _bots(code, other))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert [
        (
            source.client,
            tuple(room.room_id for room in source.rooms),
            source.target_output_dirs,
        )
        for source in call.kwargs["sources"]
    ] == [
        (
            code.client,
            ("!lobby:localhost",),
            (runtime_paths.storage_root / "agents" / "code" / "workspace" / _WORKSPACE_EXPORT_DIRNAME,),
        ),
        (
            other.client,
            ("!external:localhost",),
            (runtime_paths.storage_root / "agents" / "other" / "workspace" / _WORKSPACE_EXPORT_DIRNAME,),
        ),
    ]


async def test_invited_rooms_read_through_the_invited_entity_bot(tmp_path: Path) -> None:
    """Configured and invited rooms read through the workspace agent's bot."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    write_thread_export_matrix_state(tmp_path)
    router = _FakeBot("@mindroom_router:localhost")
    code = _FakeBot("@mindroom_code:localhost", invited_room_ids=frozenset({"!private:localhost"}))
    runner = _runner(config, _bots(router, code))
    export = _export_mock()

    with patch(EXPORT_PATH, new=export):
        runner.queue_full_pass()
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    sources = call.kwargs["sources"]
    assert [(source.client, tuple(room.room_id for room in source.rooms)) for source in sources] == [
        (code.client, ("!lobby:localhost", "!dev:localhost", "!private:localhost")),
    ]
    assert sources[0].reader.reader.hydrator.self_sender == "@mindroom_code:localhost"


@pytest.mark.parametrize("availability", ["missing", "clientless", "stopped"])
async def test_unavailable_agent_target_blocks_aliased_active_write(
    tmp_path: Path,
    availability: str,
) -> None:
    """An unavailable source still reserves its destination during overlap validation."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig()),
            "other": AgentConfig(display_name="Other", thread_exports=AgentThreadExportConfig()),
        },
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    other_agent_dir = runtime_paths.storage_root / "agents" / "other"
    stale_thread = _write_owned_export(other_agent_dir / "workspace" / _WORKSPACE_EXPORT_DIRNAME)
    code_agent_dir = runtime_paths.storage_root / "agents" / "code"
    code_agent_dir.parent.mkdir(parents=True, exist_ok=True)
    code_agent_dir.symlink_to(other_agent_dir, target_is_directory=True)
    bots: dict[str, _ThreadExportBot] = _bots(_FakeBot("@mindroom_other:localhost"))
    if availability != "missing":
        bot = _FakeBot(
            "@mindroom_code:localhost",
            running=availability != "stopped",
            client=None if availability == "clientless" else Mock(),
        )
        bots.update(_bots(bot))
    runner = _runner(config, bots)

    with patch(
        "mindroom.thread_export.service.export_threads_for_targets_for_client",
        new=AsyncMock(),
    ) as export_source:
        runner.queue_full_pass()
        await runner._run_pass_once()

    export_source.assert_not_awaited()
    assert stale_thread.exists()


async def test_full_pass_with_no_joined_rooms_preserves_existing_exports(tmp_path: Path) -> None:
    """A pass without a positive room export does not act as data erasure."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    export_dir = runtime_paths.storage_root / "agents" / "code" / "workspace" / _WORKSPACE_EXPORT_DIRNAME
    stale_thread = _write_owned_export(export_dir)
    runner = _runner(
        config,
        _bots(_FakeBot("@mindroom_code:localhost", joined_room_ids=frozenset())),
    )

    runner.queue_full_pass()
    await runner._run_pass_once()

    assert stale_thread.exists()


async def test_failed_joined_room_lookup_preserves_existing_exports(tmp_path: Path) -> None:
    """An unknown current-membership snapshot cannot authorize cleanup."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    export_dir = runtime_paths.storage_root / "agents" / "code" / "workspace" / _WORKSPACE_EXPORT_DIRNAME
    stale_thread = _write_owned_export(export_dir)
    code = _FakeBot("@mindroom_code:localhost", joined_rooms_error=RuntimeError("unavailable"))
    runner = _runner(config, _bots(code))

    runner.queue_full_pass()
    await runner._run_pass_once()

    assert stale_thread.exists()


async def test_failed_room_lookup_keeps_target_in_overlap_validation(tmp_path: Path) -> None:
    """A failed source cannot hide its target and let another source write through an alias."""
    config = _config(
        tmp_path,
        {
            "code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig()),
            "other": AgentConfig(display_name="Other", thread_exports=AgentThreadExportConfig()),
        },
    )
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    other_agent_dir = runtime_paths.storage_root / "agents" / "other"
    existing_thread = _write_owned_export(other_agent_dir / "workspace" / _WORKSPACE_EXPORT_DIRNAME)
    code_agent_dir = runtime_paths.storage_root / "agents" / "code"
    code_agent_dir.parent.mkdir(parents=True, exist_ok=True)
    code_agent_dir.symlink_to(other_agent_dir, target_is_directory=True)
    runner = _runner(
        config,
        _bots(
            _FakeBot("@mindroom_code:localhost", joined_rooms_error=RuntimeError("unavailable")),
            _FakeBot("@mindroom_other:localhost"),
        ),
    )

    with patch(
        "mindroom.thread_export.service.export_threads_for_targets_for_client",
        new=AsyncMock(),
    ) as export_source:
        runner.queue_full_pass()
        await runner._run_pass_once()

    export_source.assert_not_awaited()
    assert existing_thread.exists()


async def test_partial_pass_for_a_room_the_agent_left_preserves_existing_exports(tmp_path: Path) -> None:
    """A membership change limits future writes instead of promising data erasure."""
    config = _config(tmp_path, {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())})
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)
    export_dir = runtime_paths.storage_root / "agents" / "code" / "workspace" / _WORKSPACE_EXPORT_DIRNAME
    stale_thread = _write_owned_export(export_dir)
    runner = _runner(config, _bots(_FakeBot("@mindroom_code:localhost", joined_room_ids=frozenset())))

    runner.mark_room_activity("!lobby:localhost")
    await runner._run_pass_once()

    assert stale_thread.exists()


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
    exported = {
        target.required_member_user_ids: target.output_dir
        for target in call.kwargs["targets"]
        if target.required_member_user_ids
    }
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


async def test_unreadable_private_identity_clears_that_instance_and_keeps_the_pass_going(tmp_path: Path) -> None:
    """A private instance whose record cannot be read is cleared like any ownerless root; other targets still export."""
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
            "mindroom.private_instance_identity_store.load_private_instance_identity",
            side_effect=PermissionError("record unreadable"),
        ),
        patch(EXPORT_PATH, new=export),
    ):
        runner.mark_room_activity("!lobby:localhost")
        await runner._run_pass_once()

    call = export.await_args
    assert call is not None
    assert [
        target.required_member_user_ids for target in call.kwargs["targets"] if target.required_member_user_ids
    ] == [
        ("@mindroom_code:localhost",),
    ]
    assert not existing_thread.exists()


async def test_manual_export_borrows_live_owner_and_reserves_reload_admission(tmp_path: Path) -> None:
    """Administrative export uses the exact live client/principal and releases admission."""
    config = _config(tmp_path, {})
    write_thread_export_matrix_state(tmp_path)
    journal = EventJournalStore.open_sqlite(tmp_path / "borrowed-journal.db")
    principal = journal.principal("runtime-owned-principal")
    client = Mock(close=AsyncMock())
    bot = _FakeBot("@mindroom_router:localhost", client=client, principal=principal)
    runner = _runner(config, _bots(bot))
    gate = runner._deps.response_admission_gate

    async def export_source(**kwargs: object) -> tuple[ThreadExportAccumulator, ...]:
        assert gate.in_flight_response_count == 1
        assert kwargs["client"] is client
        reader = kwargs["reader"]
        assert reader.completeness is principal
        assert reader.reader.store is principal
        assert reader.reader.hydrator.self_sender == bot.user_id
        return tuple(ThreadExportAccumulator(target=target, rooms_exported=1) for target in kwargs["targets"])

    runner.start()
    try:
        with patch("mindroom.thread_export.service.export_threads_for_targets_for_client", side_effect=export_source):
            stats = await runner.export_once()
        assert stats.rooms_exported == 1
        assert gate.in_flight_response_count == 0
        client.close.assert_not_awaited()
        assert await principal.load_event("$missing") is None
        gate.close()
        with pytest.raises(RuntimeError, match="ready"):
            await runner.export_once()
        assert gate.in_flight_response_count == 0
    finally:
        await runner.stop()
        await journal.close()
    with pytest.raises(RuntimeError, match="running"):
        await runner.export_once()


async def test_manual_export_preserves_previous_files_when_owner_is_unavailable(tmp_path: Path) -> None:
    """Owner loss is a failed pass, never a reason to acquire another client."""
    config = _config(tmp_path, {})
    write_thread_export_matrix_state(tmp_path)
    output = tmp_path / "manual-exports"
    prior = _write_owned_export(output)
    runner = _runner(config, {})
    runner.start()
    try:
        stats = await runner.export_once(output_dir=output)
        assert stats.failures == 2
        assert all("No running Matrix owner for router" in item.error for item in stats.failed_items)
        assert prior.exists()
        assert runner._deps.response_admission_gate.in_flight_response_count == 0
    finally:
        await runner.stop()


async def test_stopping_runner_waits_for_manual_export_cleanup(tmp_path: Path) -> None:
    """A borrowed owner's lifetime cannot end before its administrative export has drained."""
    config = _config(tmp_path, {})
    runner = _runner(config, {})
    started = asyncio.Event()
    cleaning_up = asyncio.Event()
    allow_cleanup = asyncio.Event()

    async def blocked_export(**_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning_up.set()
            await allow_cleanup.wait()

    runner.start()
    with patch("mindroom.thread_export.workspace_sync.export_threads_once", side_effect=blocked_export):
        export = asyncio.create_task(runner.export_once())
        await started.wait()
        stop = asyncio.create_task(runner.stop())
        cleanup_started = asyncio.create_task(cleaning_up.wait())
        try:
            await asyncio.wait((stop, cleanup_started), return_when=asyncio.FIRST_COMPLETED)
            assert cleaning_up.is_set()
            assert not stop.done()
            assert runner._deps.response_admission_gate.in_flight_response_count == 1
            allow_cleanup.set()
            await stop
            assert export.cancelled()
            assert runner._deps.response_admission_gate.in_flight_response_count == 0
        finally:
            allow_cleanup.set()
            export.cancel()
            cleanup_started.cancel()
            await asyncio.gather(export, stop, cleanup_started, return_exceptions=True)


async def test_forced_replacement_waits_for_manual_export_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a forced replacement cannot close an owner's resources under an export."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_FORCE_AFTER_SECONDS", 0)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path, {})
    runner = orchestrator._thread_export_runner
    started = asyncio.Event()
    cleaning_up = asyncio.Event()
    allow_cleanup = asyncio.Event()
    replaced = asyncio.Event()

    async def blocked_export(**_kwargs: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning_up.set()
            await allow_cleanup.wait()

    async def replace_owner() -> None:
        assert export.cancelled()
        assert orchestrator._response_admission_gate.in_flight_response_count == 0
        replaced.set()

    runner.start()
    with patch("mindroom.thread_export.workspace_sync.export_threads_once", side_effect=blocked_export):
        export = asyncio.create_task(runner.export_once())
        await started.wait()
        replacement = asyncio.create_task(
            orchestrator.config_reload.apply_with_response_admission(
                replace_owner,
                operation_name="forced export test",
                request_is_current=lambda: True,
            ),
        )
        try:
            await asyncio.wait_for(cleaning_up.wait(), timeout=5)
            assert not replaced.is_set()
            allow_cleanup.set()
            await replacement
            assert replaced.is_set()
            assert not orchestrator._response_admission_gate.closed
        finally:
            allow_cleanup.set()
            export.cancel()
            await asyncio.gather(export, replacement, return_exceptions=True)
            await runner.stop()


@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_replacement_drains_automatic_exports_and_resumes_current_owners(  # noqa: PLR0915
    tmp_path: Path,
    outcome: str,
) -> None:
    """Borrowed clients survive automatic cleanup; interrupted work resumes after publication."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    orchestrator.config = _config(
        tmp_path,
        {"code": AgentConfig(display_name="Code", thread_exports=AgentThreadExportConfig())},
    )
    write_thread_export_matrix_state(tmp_path)
    original = _FakeBot("@mindroom_code:localhost")
    current = _FakeBot("@mindroom_code:localhost")
    orchestrator.agent_bots["code"] = cast("AgentBot", original)
    runner = orchestrator._thread_export_runner
    runner._deps = replace(runner._deps, debounce_seconds=0)
    started, cleaning, allow_cleanup, cleaned = (asyncio.Event() for _ in range(4))
    publishing, allow_publication, resumed = (asyncio.Event() for _ in range(3))
    output = tmp_path / "exported.txt"

    async def export_sources(**kwargs: object) -> tuple[ThreadExportStats, ...]:
        sources = cast("Sequence[ThreadExportSource]", kwargs["sources"])
        if sources[0].client is original.client:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await allow_cleanup.wait()
                cleaned.set()
        assert sources[0].client is current.client
        assert {room.room_id for source in sources for room in source.rooms} == {"!lobby:localhost", "!dev:localhost"}
        assert not orchestrator._response_admission_gate.closed
        output.write_text("exported using the replacement owner")
        resumed.set()
        return _stats_for_targets(**kwargs)

    async def publish() -> None:
        assert cleaned.is_set(), "replacement closed a client still borrowed by an automatic export"
        orchestrator.agent_bots["code"] = cast("AgentBot", current)
        runner.start()  # Runtime worker synchronization also calls start during publication.
        if outcome != "success":
            runner.mark_room_activity("!lobby:localhost")
        publishing.set()
        await allow_publication.wait()
        if outcome == "error":
            msg = "publication interrupted after replacing the owner"
            raise RuntimeError(msg)

    with patch(EXPORT_PATH, side_effect=export_sources):
        runner.start()
        runner.mark_room_activity("!lobby:localhost")
        await asyncio.wait_for(started.wait(), timeout=5)
        replacement = asyncio.create_task(
            orchestrator.config_reload.apply_with_response_admission(
                publish,
                operation_name="automatic export replacement",
                request_is_current=lambda: True,
            ),
        )
        try:
            await asyncio.wait_for(cleaning.wait(), timeout=5)
            assert not publishing.is_set()
            allow_cleanup.set()
            await asyncio.wait_for(publishing.wait(), timeout=5)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(resumed.wait(), timeout=0.05)
            assert not output.exists()
            if outcome == "cancelled":
                replacement.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await replacement
            elif outcome == "error":
                allow_publication.set()
                with pytest.raises(RuntimeError, match="publication interrupted"):
                    await replacement
            else:
                allow_publication.set()
                await replacement
            await asyncio.wait_for(resumed.wait(), timeout=5)
            assert output.read_text() == "exported using the replacement owner"
        finally:
            allow_cleanup.set()
            allow_publication.set()
            await asyncio.gather(replacement, return_exceptions=True)
            await runner.stop()
