"""Approval continuations clean up when a step fails: a team resume finalizes consumption, an agent one closes its run."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from mindroom.approval.execution import _collect_agent_continuation
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.llm.events import ContentDelta
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.session_storage.types import ConversationSession
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.events import CollectedStreamPresentation, StructuredStreamChunk
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.events import RunEvent


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
async def test_team_approval_finalizes_consumption_after_member_assembly_fails(tmp_path: Path) -> None:
    """A team resume whose members cannot be assembled still finalizes consumption, and that failure surfaces."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    identity = _identity()
    scope = ScopeSessionContext(
        scope=HistoryScope(kind="team", scope_id="research"),
        storage=MagicMock(),
        session=ConversationSession(
            session_id="session-1",
            kind="team",
            owner_id="research",
            runs=[TeamRunRecord(run_id="run-1", session_id="session-1", status=RunStatus.paused)],
        ),
        session_id="session-1",
    )
    finalized: list[bool] = []

    async def fail_finalization() -> None:
        finalized.append(True)
        msg = "consumption finalization failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.teams.resume.load_scope_session", return_value=scope),
        patch("mindroom.teams.resume.materialize_exact_team_members", side_effect=RuntimeError("assembly failed")),
        patch("mindroom.teams.resume.finalize_consumption", new=fail_finalization),
        pytest.raises(RuntimeError, match="consumption finalization failed"),
    ):
        await continue_paused_team_run(
            member_names=("general",),
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
            progress=None,
        )

    assert finalized == [True]


@pytest.mark.asyncio
async def test_an_agent_continuation_whose_publication_fails_closes_its_run_stream() -> None:
    """A stop that lands while the resumed reply publishes stops the resumed run then, not when it is collected."""
    closed: list[bool] = []

    async def events() -> AsyncIterator[RunEvent]:
        try:
            yield ContentDelta("Working.")
            yield ContentDelta(" More.")
        finally:
            closed.append(True)

    published: list[StructuredStreamChunk] = []

    async def progress(chunk: StructuredStreamChunk) -> None:
        published.append(chunk)
        if len(published) > 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _collect_agent_continuation(
            events(),
            CollectedStreamPresentation(show_tool_calls=True),
            progress=progress,
        )
    assert closed == [True]
