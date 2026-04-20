"""Canonical interrupted-turn replay helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agents import get_agent_session, get_team_session
from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SEEN_EVENT_IDS_METADATA_KEY
from mindroom.tool_system.events import ToolTraceEntry, render_tool_trace_for_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.db.sqlite import SqliteDb

    from mindroom.history.runtime import ScopeSessionContext

_INTERRUPTED_REPLAY_STATE_KEY = "mindroom_replay_state"
_ORIGINAL_STATUS_KEY = "mindroom_original_status"
_INTERRUPTED_REPLAY_STATE = "interrupted"
_INTERRUPTED_RESPONSE_MARKER = "[interrupted]"
_MATRIX_RESPONSE_EVENT_ID_METADATA_KEY = "matrix_response_event_id"


@dataclass(frozen=True)
class InterruptedReplaySnapshot:
    """Trusted interrupted self-turn facts needed for canonical replay."""

    user_message: str
    partial_text: str
    completed_tools: tuple[ToolTraceEntry, ...]
    interrupted_tools: tuple[ToolTraceEntry, ...]
    seen_event_ids: tuple[str, ...]
    source_event_id: str | None
    response_event_id: str | None
    interruption_reason: str


def _render_interrupted_tool_trace(events: Sequence[ToolTraceEntry]) -> str:
    lines: list[str] = []
    for event in events:
        lines.append(f"[tool:{event.tool_name} interrupted]")
        if event.args_preview:
            lines.append(f"  args: {event.args_preview}")
        lines.append("  result: <interrupted before completion>")
        if event.truncated:
            lines.append("  (truncated)")
    return "\n".join(lines)


def render_interrupted_replay_content(snapshot: InterruptedReplaySnapshot) -> str:
    """Render one interrupted snapshot into canonical assistant replay text."""
    parts: list[str] = []
    if snapshot.partial_text:
        parts.append(snapshot.partial_text)
    tool_parts: list[str] = []
    if snapshot.completed_tools:
        tool_parts.append(render_tool_trace_for_context(list(snapshot.completed_tools)))
    if snapshot.interrupted_tools:
        tool_parts.append(_render_interrupted_tool_trace(snapshot.interrupted_tools))
    if tool_parts:
        parts.append("\n".join(tool_parts))
    parts.append(_INTERRUPTED_RESPONSE_MARKER)
    return "\n\n".join(part for part in parts if part)


def _interrupted_replay_metadata(snapshot: InterruptedReplaySnapshot) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        MATRIX_SEEN_EVENT_IDS_METADATA_KEY: list(snapshot.seen_event_ids),
        _ORIGINAL_STATUS_KEY: "cancelled",
        _INTERRUPTED_REPLAY_STATE_KEY: _INTERRUPTED_REPLAY_STATE,
    }
    if snapshot.source_event_id is not None:
        metadata[MATRIX_EVENT_ID_METADATA_KEY] = snapshot.source_event_id
    if snapshot.response_event_id is not None:
        metadata[_MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = snapshot.response_event_id
    return metadata


def build_interrupted_replay_run(
    *,
    snapshot: InterruptedReplaySnapshot,
    run_id: str,
    scope_id: str,
    session_id: str,
    is_team: bool,
) -> RunOutput | TeamRunOutput:
    """Build one canonical replayable run for an interrupted top-level turn."""
    content = render_interrupted_replay_content(snapshot)
    messages = []
    if snapshot.user_message:
        messages.append(Message(role="user", content=snapshot.user_message))
    messages.append(Message(role="assistant", content=content))
    metadata = _interrupted_replay_metadata(snapshot)
    if is_team:
        return TeamRunOutput(
            run_id=run_id,
            team_id=scope_id,
            session_id=session_id,
            content=content,
            messages=messages,
            metadata=metadata,
            status=RunStatus.completed,
        )
    return RunOutput(
        run_id=run_id,
        agent_id=scope_id,
        session_id=session_id,
        content=content,
        messages=messages,
        metadata=metadata,
        status=RunStatus.completed,
    )


def build_interrupted_replay_snapshot(
    *,
    user_message: str | None,
    partial_text: str | None,
    completed_tools: Sequence[ToolTraceEntry],
    interrupted_tools: Sequence[ToolTraceEntry],
    run_metadata: Mapping[str, object] | None,
    interruption_reason: str,
) -> InterruptedReplaySnapshot:
    """Build one canonical interrupted replay snapshot from trusted runtime state."""
    metadata = run_metadata if isinstance(run_metadata, Mapping) else {}
    raw_seen_event_ids = metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    seen_event_ids = (
        tuple(event_id for event_id in raw_seen_event_ids if isinstance(event_id, str) and event_id)
        if isinstance(raw_seen_event_ids, list)
        else ()
    )
    source_event_id = metadata.get(MATRIX_EVENT_ID_METADATA_KEY)
    response_event_id = metadata.get(_MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    return InterruptedReplaySnapshot(
        user_message=(user_message or "").strip(),
        partial_text=(partial_text or "").strip(),
        completed_tools=tuple(completed_tools),
        interrupted_tools=tuple(interrupted_tools),
        seen_event_ids=seen_event_ids,
        source_event_id=source_event_id if isinstance(source_event_id, str) and source_event_id else None,
        response_event_id=response_event_id if isinstance(response_event_id, str) and response_event_id else None,
        interruption_reason=interruption_reason,
    )


def persist_interrupted_replay_snapshot(
    *,
    storage: SqliteDb,
    session: AgentSession | TeamSession | None,
    session_id: str,
    scope_id: str,
    run_id: str,
    snapshot: InterruptedReplaySnapshot,
    is_team: bool,
) -> None:
    """Persist one canonical interrupted replay snapshot into session history."""
    persisted_session = _load_persisted_session(
        storage=storage,
        session_id=session_id,
        is_team=is_team,
    )
    if persisted_session is None:
        persisted_session = session
    if persisted_session is None:
        persisted_session = _new_session(
            session_id=session_id,
            scope_id=scope_id,
            is_team=is_team,
        )
    persisted_run = build_interrupted_replay_run(
        snapshot=snapshot,
        run_id=run_id,
        scope_id=scope_id,
        session_id=session_id,
        is_team=is_team,
    )
    if is_team:
        assert isinstance(persisted_session, TeamSession)
        assert isinstance(persisted_run, TeamRunOutput)
        persisted_session.upsert_run(persisted_run)
    else:
        assert isinstance(persisted_session, AgentSession)
        assert isinstance(persisted_run, RunOutput)
        persisted_session.upsert_run(persisted_run)
    storage.upsert_session(persisted_session)


def persist_interrupted_replay(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str,
    run_id: str,
    user_message: str | None,
    partial_text: str | None,
    completed_tools: Sequence[ToolTraceEntry],
    interrupted_tools: Sequence[ToolTraceEntry],
    run_metadata: Mapping[str, object] | None,
    interruption_reason: str,
    is_team: bool,
) -> None:
    """Persist one interrupted top-level turn from trusted runtime state."""
    if scope_context is None:
        return
    persist_interrupted_replay_snapshot(
        storage=scope_context.storage,
        session=scope_context.session,
        session_id=session_id,
        scope_id=scope_context.scope.scope_id,
        run_id=run_id,
        snapshot=build_interrupted_replay_snapshot(
            user_message=user_message,
            partial_text=partial_text,
            completed_tools=completed_tools,
            interrupted_tools=interrupted_tools,
            run_metadata=run_metadata,
            interruption_reason=interruption_reason,
        ),
        is_team=is_team,
    )


def _load_persisted_session(
    *,
    storage: SqliteDb,
    session_id: str,
    is_team: bool,
) -> AgentSession | TeamSession | None:
    if is_team:
        return get_team_session(storage, session_id)
    return get_agent_session(storage, session_id)


def _new_session(
    *,
    session_id: str,
    scope_id: str,
    is_team: bool,
) -> AgentSession | TeamSession:
    created_at = int(datetime.now(UTC).timestamp())
    if is_team:
        return TeamSession(
            session_id=session_id,
            team_id=scope_id,
            metadata={},
            runs=[],
            created_at=created_at,
            updated_at=created_at,
        )
    return AgentSession(
        session_id=session_id,
        agent_id=scope_id,
        metadata={},
        runs=[],
        created_at=created_at,
        updated_at=created_at,
    )
