"""An agent's session history and learnings, written like MindRoom writes them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.db.sqlite import SqliteDb
from agno.metrics import RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput, RunStartedEvent
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import (
    add_pending_force_compaction_scope,
    read_scope_state,
    set_force_compaction_state,
    update_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import (
    create_state_storage,
)
from mindroom.tool_system.worker_routing import agent_state_root_path
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.constants import RuntimePaths


ALICE = "@alice:x"
SESSION = "s1"


def agent_root(runtime_paths: RuntimePaths, agent: str) -> Path:
    """Return the agent's state root, resolved like ``resolve_agent_storage`` resolves it, so links are followed."""
    return agent_state_root_path(runtime_paths.storage_root, agent).resolve()


def session_storage(runtime_paths: RuntimePaths, agent: str) -> BaseDb:
    """Open the agent's session store the way MindRoom opens it."""
    return create_state_storage(
        agent,
        agent_root(runtime_paths, agent),
        subdir="sessions",
        session_table=f"{agent}_sessions",
    )


def learning_storage(runtime_paths: RuntimePaths, agent: str) -> SqliteDb:
    """Open the agent's learning store the way MindRoom opens it."""
    storage = create_state_storage(
        agent,
        agent_root(runtime_paths, agent),
        subdir="learning",
        session_table=f"{agent}_learning_sessions",
    )
    assert isinstance(storage, SqliteDb)
    return storage


def agent_run(agent: str, run_id: str) -> RunOutput:
    """Return a completed run of the agent with messages, metrics, an event and seen event ids."""
    return RunOutput(
        run_id=run_id,
        agent_id=agent,
        agent_name=agent.title(),
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        model="test-model",
        model_provider="test-provider",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}"), Message(role="assistant", content="answer")],
        metrics=RunMetrics(input_tokens=2, output_tokens=3, total_tokens=5),
        events=[RunStartedEvent(agent_id=agent, run_id=run_id, session_id=SESSION)],
        metadata={constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"]},
    )


def seed_agent(runtime_paths: RuntimePaths, agent: str) -> None:
    """Write an agent's history and learnings the way MindRoom does: two runs, the first summarized."""
    scope = HistoryScope(kind="agent", scope_id=agent)
    session = AgentSession(
        session_id=SESSION,
        agent_id=agent,
        user_id=ALICE,
        # Written like agno/agent/_storage.py's get_agent_data.
        agent_data={"name": agent.title(), "agent_id": agent},
        # An agent's own session state may hold any value, even one spelled like a scope key.
        session_data={"session_state": add_pending_force_compaction_scope({"target": f"agent:{agent}"}, scope)},
        metadata={},
        runs=[agent_run(agent, "r1"), agent_run(agent, "r2")],
    )
    update_scope_seen_event_ids(session, scope, ["$preserved"])
    set_force_compaction_state(session, scope, read_scope_state(session, scope), force=True)
    # Another scope whose name extends this agent's keeps its own state.
    update_scope_seen_event_ids(session, HistoryScope(kind="agent", scope_id=f"{agent}_team"), ["$other"])
    storage = session_storage(runtime_paths, agent)
    try:
        seed_session(storage, session)
        covered = summary_rows.covered_runs(storage, session_id=SESSION, run_ids=["r1"])
        summary_rows.append_summary(
            storage,
            session_id=SESSION,
            scope_key=scope.key,
            based_on=None,
            covered=covered,
            summary=f"summary of {agent}'s r1",
            summary_model="test-model",
        )
    finally:
        storage.close()
    learning = learning_storage(runtime_paths, agent)
    try:
        learning.upsert_learning(
            id=f"user_memory_{agent}",
            learning_type="user_memory",
            content={"memories": [f"{agent} learned this"]},
            user_id=ALICE,
            agent_id=agent,
        )
        # Creates the learning store's sessions table, which the rename covers too.
        learning.upsert_session(AgentSession(session_id=SESSION, agent_id=agent, user_id=ALICE))
    finally:
        learning.close()
