"""An agent's session history, written like MindRoom writes it, with the fields Agno-era releases left in it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.storage import (
    add_pending_force_compaction_scope,
    set_force_compaction,
    update_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import agent_state_root_path
from mindroom.usage.storage import quote_identifier
from tests.conftest import persist_session

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore


ALICE = "@alice:x"
SESSION = "s1"


def agent_root(runtime_paths: RuntimePaths, agent: str) -> Path:
    """Return the agent's state root, resolved like ``resolve_agent_storage`` resolves it, so links are followed."""
    return agent_state_root_path(runtime_paths.storage_root, agent).resolve()


def session_storage(runtime_paths: RuntimePaths, agent: str) -> ConversationStore:
    """Open the agent's session store the way MindRoom opens it."""
    return create_state_storage(agent, agent_root(runtime_paths, agent))


def agent_run(agent: str, run_id: str) -> RunRecord:
    """Return a completed run of the agent with messages, metrics and seen event ids."""
    return RunRecord(
        run_id=run_id,
        agent_id=agent,
        agent_display_name=agent.title(),
        session_id=SESSION,
        user_id=ALICE,
        status=RunStatus.completed,
        model="test-model",
        model_provider="test-provider",
        content=f"answer {run_id}",
        messages=[Message(role="user", content=f"question {run_id}"), Message(role="assistant", content="answer")],
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=2, output_tokens=3, total_tokens=5)),
        metadata={constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"]},
    )


def agent_session(agent: str, *runs: RunRecord, session_state: dict[str, object] | None = None) -> ConversationSession:
    """Return the agent's session ``SESSION`` holding ``runs``."""
    return ConversationSession(
        session_id=SESSION,
        kind="agent",
        owner_id=agent,
        session_state=session_state or {},
        runs=list(runs),
    )


def agno_era_run(run_data: dict[str, object]) -> dict[str, object]:
    """Return ``run_data`` with the started event Agno-era releases stored in a run and in each of its member runs."""
    owner_field = "agent_id" if "agent_id" in run_data else "team_id"
    event = {"event": "RunStarted", owner_field: run_data[owner_field], "run_id": run_data["run_id"]}
    members = run_data.get("member_responses")
    renamed = {**run_data, "events": [event]}
    if isinstance(members, list):
        renamed["member_responses"] = [agno_era_run(member) for member in members]
    return renamed


def add_agno_era_fields(database: Path, table: str, *, owner_field: str, owner: str, display_name: str) -> None:
    """Add what Agno-era releases left in a session database that MindRoom kept writing after Agno's removal.

    Agno recorded each table's schema version in ``agno_schema_versions``, stored the owner's
    ``agent_data``/``team_data`` on the session row, and kept the started event in every run's JSON.
    """
    data_column = "team_data" if owner_field == "team_id" else "agent_data"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE agno_schema_versions (table_name VARCHAR NOT NULL PRIMARY KEY, version VARCHAR NOT NULL, "
            "created_at BIGINT NOT NULL, updated_at BIGINT)",
        )
        connection.executemany(
            "INSERT INTO agno_schema_versions (table_name, version, created_at) VALUES (?, '2.0.0', 0)",
            [(table,), (f"{table}_runs",)],
        )
        connection.execute(
            f"UPDATE {quote_identifier(table)} SET {data_column} = ?",  # noqa: S608
            (json.dumps({"name": display_name, owner_field: owner}),),
        )
        runs = quote_identifier(f"{table}_runs")
        for run_id, run_data in connection.execute(f"SELECT run_id, run_data FROM {runs}").fetchall():  # noqa: S608
            connection.execute(
                f"UPDATE {runs} SET run_data = ? WHERE run_id = ?",  # noqa: S608
                (json.dumps(agno_era_run(json.loads(run_data))), run_id),
            )
        connection.commit()


def seed_agent(runtime_paths: RuntimePaths, agent: str) -> None:
    """Write an agent's history the way MindRoom does: two runs with their usage, the first summarized."""
    scope = HistoryScope(kind="agent", scope_id=agent)
    # An agent's own session state may hold any value, even one spelled like a scope key.
    session = agent_session(
        agent,
        agent_run(agent, "r1"),
        agent_run(agent, "r2"),
        session_state=add_pending_force_compaction_scope({"target": f"agent:{agent}"}, scope),
    )
    storage = session_storage(runtime_paths, agent)
    persist_session(storage, session)
    update_scope_seen_event_ids(storage, session, scope, ["$preserved"])
    set_force_compaction(storage, session=session, scope=scope, forced=True)
    # Another scope whose name extends this agent's keeps its own state.
    update_scope_seen_event_ids(storage, session, HistoryScope(kind="agent", scope_id=f"{agent}_team"), ["$other"])
    covered = summary_rows.covered_runs(storage, session_id=SESSION, run_ids=["r1"])
    summary_rows.append_summary(
        storage,
        session=session,
        scope_key=scope.key,
        based_on=None,
        covered=covered,
        summary=f"summary of {agent}'s r1",
        summary_model="test-model",
    )
    add_agno_era_fields(
        storage.path,
        storage.names.sessions,
        owner_field="agent_id",
        owner=agent,
        display_name=agent.title(),
    )
