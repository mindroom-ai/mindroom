"""Persist compact active-turn continuation checkpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.agent_storage import get_agent_session
from mindroom.history.storage import new_scope_session

if TYPE_CHECKING:
    from agno.db.base import BaseDb

    from mindroom.active_turn_checkpoint import ActiveTurnCheckpoint

_CHECKPOINT_STATE_KEY = "mindroom_replay_state"
_CHECKPOINT_STATE = "active_turn_checkpoint"


def persist_continuation_checkpoint(
    *,
    storage: BaseDb,
    session: AgentSession | None,
    session_id: str,
    scope_id: str,
    run_id: str,
    checkpoint: ActiveTurnCheckpoint,
    run_metadata: dict[str, Any] | None,
) -> None:
    """Replace one raw completed run with a compact replay-safe checkpoint."""
    persisted_session = get_agent_session(storage, session_id)
    if persisted_session is None:
        persisted_session = session
    if persisted_session is None:
        persisted_session = new_scope_session(
            session_id=session_id,
            scope_id=scope_id,
            is_team=False,
        )
    assert isinstance(persisted_session, AgentSession)
    persisted_run = _build_checkpoint_run(
        checkpoint=checkpoint,
        run_id=run_id,
        scope_id=scope_id,
        session_id=session_id,
        run_metadata=run_metadata,
    )
    persisted_session.upsert_run(persisted_run)
    if session is not None and session is not persisted_session:
        session.upsert_run(persisted_run)
    storage.upsert_session(persisted_session)


def _build_checkpoint_run(
    *,
    checkpoint: ActiveTurnCheckpoint,
    run_id: str,
    scope_id: str,
    session_id: str,
    run_metadata: dict[str, Any] | None,
) -> RunOutput:
    metadata = dict(run_metadata or {})
    metadata[_CHECKPOINT_STATE_KEY] = _CHECKPOINT_STATE
    metadata["mindroom_active_turn_checkpoint"] = {
        "version": 1,
        "estimated_input_tokens": checkpoint.trigger.estimated_input_tokens,
        "input_limit_tokens": checkpoint.trigger.input_limit_tokens,
        "context_window_tokens": checkpoint.trigger.context_window_tokens,
        "used_actual_input_tokens": checkpoint.trigger.used_actual_input_tokens,
    }
    messages = [
        Message(role="user", content="Continue the saved goal from the active-turn checkpoint."),
        Message(role="assistant", content=checkpoint.content),
    ]
    return RunOutput(
        run_id=run_id,
        agent_id=scope_id,
        session_id=session_id,
        content=checkpoint.content,
        messages=messages,
        metadata=metadata,
        status=RunStatus.completed,
    )
