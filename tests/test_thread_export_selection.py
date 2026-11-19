"""Tests for thread-export room selection."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.matrix.identity import MatrixID
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.room_steward import room_steward
from mindroom.thread_export.models import ThreadExportAccumulator, ThreadExportGroup, ThreadExportRoom
from mindroom.thread_export.selection import build_export_groups, export_rooms
from mindroom.thread_export.workspace_sync import WorkspaceThreadExportDeps, WorkspaceThreadExportRunner
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.thread_export_helpers import thread_export_config, write_thread_export_matrix_state

if TYPE_CHECKING:
    from pathlib import Path


def test_export_rooms_filters_by_room_metadata_substring(tmp_path: Path) -> None:
    """Room filtering should match substrings across user-facing room fields."""
    config = thread_export_config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    write_thread_export_matrix_state(tmp_path)

    assert [room.key for room in export_rooms(runtime_paths, "obb")] == ["lobby"]
    assert {room.key for room in export_rooms(runtime_paths, "LOCALHOST")} == {"lobby", "dev"}


def test_export_groups_use_runtime_entities_without_reading_credentials() -> None:
    """Each configured room is read by its own steward; invited rooms use their own entity."""
    configured = ThreadExportRoom("lobby", "!lobby:localhost", "", "Lobby")
    dev = ThreadExportRoom("dev", "!dev:localhost", "", "Dev")
    invited = ThreadExportRoom("!invited:localhost", "!invited:localhost", "", "", invited=True)
    assert build_export_groups(state_rooms=[configured, dev], invited_groups=[("general", [invited])]) == [
        ThreadExportGroup(rooms=(configured,), entity_name=None),
        ThreadExportGroup(rooms=(dev,), entity_name=None),
        ThreadExportGroup(rooms=(invited,), entity_name="general"),
    ]


@pytest.mark.asyncio
async def test_export_reads_through_joined_agent(tmp_path: Path) -> None:
    """With one person, one agent and no owner account, a room reads through the agent; a room no bot joined fails."""
    config = bind_runtime_paths(Config(agents={}), test_runtime_paths(tmp_path))
    write_thread_export_matrix_state(tmp_path)
    agent_id = "@mindroom_code:localhost"
    lobby = nio.MatrixRoom("!lobby:localhost", agent_id)
    lobby.add_member("@alice:localhost", "Alice", None)
    lobby.add_member(agent_id, "Code", None)
    client = Mock(user_id=agent_id, rooms={lobby.room_id: lobby})
    bots = {"code": Mock(agent_name="code", running=True, client=client, matrix_id=MatrixID.parse(agent_id))}
    runner = WorkspaceThreadExportRunner(
        WorkspaceThreadExportDeps(
            runtime_paths=runtime_paths_for(config),
            config_provider=lambda: config,
            bot_provider=bots.get,
            steward_provider=partial(room_steward, bots),
            response_admission_gate=ResponseAdmissionGate(),
            agent_reply_memberships=AgentReplyMembershipIndex(),
            debounce_seconds=0,
        ),
    )
    export = AsyncMock(
        side_effect=lambda **kwargs: tuple(
            ThreadExportAccumulator(target=target, rooms_exported=1) for target in kwargs["targets"]
        ),
    )
    await runner.sync(config)
    try:
        with patch("mindroom.thread_export.service.export_threads_for_targets_for_client", new=export):
            stats = await runner.export_once(output_dir=tmp_path / "exports")
    finally:
        await runner.stop()

    assert [
        (call.kwargs["client"], call.kwargs["entity_name"], [room.room_id for room in call.kwargs["rooms"]])
        for call in export.await_args_list
    ] == [(client, "code", ["!lobby:localhost"])]
    assert stats.rooms_exported == 1
    assert ["No running Matrix owner for !dev:localhost" in item.error for item in stats.failed_items] == [True]
