"""Open the conversation store of an agent, a team scope, or the system usage ledger, migrating the database first."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.constants import prompt_roles_for_history_storage
from mindroom.history.legacy_compaction_state import migrate_to_summary_rows
from mindroom.runtime_resolution import record_private_instance_owner, resolve_agent_storage
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.store import ConversationStore, open_conversation_store
from mindroom.usage.legacy_usage_storage import migrate_usage_database

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


_BUSY_TIMEOUT_SECONDS = 30.0


def create_state_storage(storage_name: str, state_root: Path, *, session_table: str) -> ConversationStore:
    """Open the conversation database ``<state_root>/sessions/<storage_name>.db`` with its migrations applied.

    Under the recovery lock, in order: a database missing required session columns is archived, an older
    database gains its usage ledger, then its summary rows, and only then does the owned store open it. Every
    store leaves the prompt roles out of the runs it writes.
    """
    with session_storage_preflight(
        state_root,
        storage_name=storage_name,
        session_table=session_table,
        timeout_seconds=_BUSY_TIMEOUT_SECONDS,
    ):
        db_dir = state_root / "sessions"
        db_dir.mkdir(parents=True, exist_ok=True)
        db_path = db_dir / f"{storage_name}.db"
        names = TableNames(session_table)
        migrate_usage_database(db_path, session_table)
        migrate_to_summary_rows(db_path, names)
        return open_conversation_store(db_path, names, prompt_roles=prompt_roles_for_history_storage())


def create_session_storage(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> ConversationStore:
    """Open one agent's conversation store, in the private state root its execution identity resolves to."""
    resolved_storage = resolve_agent_storage(agent_name, config, runtime_paths, execution_identity=execution_identity)
    # A turn can read its session before it builds the agent, which is the other place this record is written.
    record_private_instance_owner(agent_name, resolved_storage.execution, runtime_paths)
    return create_state_storage(
        agent_name,
        resolved_storage.session_state_root,
        session_table=f"{agent_name}_sessions",
    )


def save_compaction_usage(
    store: ConversationStore,
    *,
    session_id: str,
    requester_id: str | None,
    model: ModelIdentity,
    usage: MessageMetrics,
    initial_session: ConversationSession | None,
) -> None:
    """Persist one summary response's returned counters independently of conversation changes."""
    metrics = usage.to_dict()
    created_at = time.time()
    store.save_independent_usage(
        session_id=session_id,
        usage_id=f"compaction:{uuid4()}",
        kind="compaction_summary",
        requester_id=requester_id,
        initial_session=initial_session,
        run={
            "model_provider": model.provider_name,
            "model": model.id,
            "created_at": created_at,
            "metrics": metrics,
            "messages": [{"role": "assistant", "created_at": created_at, "metrics": dict(metrics)}],
        },
    )


__all__ = [
    "create_session_storage",
    "create_state_storage",
    "save_compaction_usage",
]
