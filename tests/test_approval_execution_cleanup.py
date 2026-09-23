"""Approval reconstruction closes owned storage when consumption finalization fails."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.approval_execution import AgentApprovalExecution
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.event_journal import ApprovalContinuation
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.response_sources import ResponseSources
from mindroom.teams import TeamMode, continue_paused_team_run
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _identity() -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )


def _config() -> Config:
    return Config(agents={"general": AgentConfig(display_name="General", rooms=[])})


@pytest.mark.asyncio
async def test_agent_approval_closes_storage_after_consumption_finalization_error(tmp_path: Path) -> None:
    """A failed consumption finalizer cannot skip reconstructed agent database closure."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    identity = _identity()
    paused = RunOutput(run_id="run-1", agent_id="general", session_id="session-1", status=RunStatus.paused)
    session = AgentSession(session_id="session-1", agent_id="general", user_id=identity.requester_id, runs=[paused])
    cleanup_order: list[str] = []

    class RecordingDb(SqliteDb):
        def __init__(self, label: str) -> None:
            super().__init__(db_file=str(tmp_path / f"{label}.db"))
            self.label = label

        def close(self) -> None:
            cleanup_order.append(self.label)
            super().close()

    history_storage = RecordingDb("storage")
    history_storage.upsert_session(session)
    history_storage.upsert_run(paused, session_id=session.session_id, user_id=identity.requester_id)
    agent = Agent(id="general", db=RecordingDb("agent"))

    async def fail_finalization() -> None:
        cleanup_order.append("finalize")
        msg = "consumption finalization failed"
        raise RuntimeError(msg)

    execution = AgentApprovalExecution(
        config=lambda: config,
        runtime_paths=paths,
        client=MagicMock(),
        tool_runtime=MagicMock(),
        knowledge_access=MagicMock(
            resolve_for_agent_async=AsyncMock(return_value=SimpleNamespace(knowledge=None)),
        ),
        refresh_scheduler=lambda: None,
    )
    continuation = ApprovalContinuation(
        approval_id="approval-1",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id=identity.room_id or "",
        thread_id=identity.thread_id,
        requester_id=identity.requester_id,
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state="claimed",
    )
    with (
        patch("mindroom.approval_execution.create_session_storage", return_value=history_storage),
        patch("mindroom.approval_execution.create_agent", return_value=agent),
        patch("mindroom.approval_execution.restore_native_history", side_effect=RuntimeError("continuation failed")),
        patch("mindroom.approval_execution.finalize_consumption", new=fail_finalization),
        pytest.raises(RuntimeError, match="consumption finalization failed"),
    ):
        await execution.continue_run(
            continuation,
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={},
            denial_reasons={},
            tool_trace_collector=[],
            typing_log_context={},
        )

    assert cleanup_order == ["finalize", "agent", "storage"]


@pytest.mark.asyncio
async def test_team_approval_closes_scope_after_consumption_finalization_error(tmp_path: Path) -> None:
    """A failed consumption finalizer cannot skip reconstructed team scope closure."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    identity = _identity()
    storage = MagicMock()
    scope = ScopeSessionContext(
        scope=HistoryScope(kind="team", scope_id="research"),
        storage=storage,
        session=TeamSession(
            session_id="session-1",
            team_id="research",
            user_id=identity.requester_id,
            runs=[TeamRunOutput(run_id="run-1", session_id="session-1", status=RunStatus.paused)],
        ),
        session_id="session-1",
    )
    cleanup_order: list[str] = []

    @contextmanager
    def open_scope() -> Iterator[ScopeSessionContext]:
        try:
            yield scope
        finally:
            cleanup_order.append("scope")

    async def fail_finalization() -> None:
        cleanup_order.append("finalize")
        msg = "consumption finalization failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.teams.open_resolved_scope_session_context", return_value=open_scope()),
        patch("mindroom.teams.materialize_exact_team_members", side_effect=RuntimeError("assembly failed")),
        patch("mindroom.teams.finalize_consumption", new=fail_finalization),
        patch("mindroom.teams._register_team_notice_storage"),
        patch(
            "mindroom.teams.close_team_runtime_state_dbs",
            side_effect=lambda **_kwargs: cleanup_order.append("team"),
        ),
        pytest.raises(RuntimeError, match="consumption finalization failed"),
    ):
        await continue_paused_team_run(
            member_names=(),
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            session_id="session-1",
            run_id="run-1",
            user_id=identity.requester_id,
            configured_team_name="research",
            model_name="default",
            decisions={},
            denial_reasons={},
            refresh_scheduler=None,
            history_scope=scope.scope,
        )

    assert cleanup_order == ["finalize", "team", "scope"]
