"""Open the conversation store of an agent, a team scope, or the system usage ledger, converting the database first."""

from __future__ import annotations

import time
from contextlib import ExitStack, closing
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.runtime_resolution import record_private_instance_owner, resolve_agent_storage
from mindroom.session_storage.legacy_agent_session_owners import upgrade_storage_v2
from mindroom.session_storage.legacy_agno3_conversion import convert_conversation_database
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import TableNames, is_current_layout, log_schema_sql, table_names
from mindroom.session_storage.store import BUSY_TIMEOUT_SECONDS, ConversationStore, begun, connect_by_name

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def create_state_storage(storage_name: str, state_root: Path) -> ConversationStore:
    """Open the conversation database ``<state_root>/sessions/<storage_name>.db``, converted to the current layout.

    Under the recovery lock, in order: a database missing required session columns is archived, an Agno-layout
    database converts, a new one gets every table, and only then does the conversation store open it. An agent's
    database opens through ``create_agent_state_storage`` instead.
    """
    return _open_state_storage(storage_name, state_root, agent_name=None)


def create_agent_state_storage(agent_name: str, state_root: Path) -> ConversationStore:
    """Open agent ``agent_name``'s conversation database as ``create_state_storage`` does, its session rows naming it."""
    return _open_state_storage(agent_name, state_root, agent_name=agent_name)


def _open_state_storage(storage_name: str, state_root: Path, *, agent_name: str | None) -> ConversationStore:
    names = TableNames(storage_name)
    db_path = names.database_path(state_root)
    with session_storage_preflight(state_root, names, timeout_seconds=BUSY_TIMEOUT_SECONDS):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        prepare_conversation_database(db_path, names, agent_name=agent_name)
        return ConversationStore(db_path)


def prepare_conversation_database(path: Path, names: TableNames, *, agent_name: str | None) -> None:
    """Bring the database at ``path`` to the current layout before a store opens it.

    The connection follows no link to the file (``connect_by_name``). A database in the current layout costs one read,
    a new or empty one gets every table, and only one that holds older tables is handed to the one-way conversion.
    ``agent_name`` names the agent whose database it is; only then does a storage v2 database reach the current
    version (``upgrade_storage_v2``), which costs one more read once it has.
    """
    with ExitStack() as descriptors, closing(connect_by_name(path)) as connection:
        if is_current_layout(connection, names):
            if agent_name is not None:
                upgrade_storage_v2(connection, names, agent_name)
            return
        if not table_names(connection):
            _create_tables(connection, names)
        if not is_current_layout(connection, names):
            convert_conversation_database(connection, path, names, descriptors, agent_name=agent_name)


def _create_tables(connection: sqlite3.Connection, names: TableNames) -> None:
    # Asked again under the lock: another opener may have created or converted the file meanwhile.
    with begun(connection, write=True):
        if not table_names(connection):
            for statement in log_schema_sql(names):
                connection.execute(statement)


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
    return create_agent_state_storage(agent_name, resolved_storage.session_state_root)


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
    # TODO(simplify): this summary request records no model provenance, so reports attribute it by the legacy rule.
    # Stamp its `provider_data` with the model, as the other usage writers do. Stored usage ledger rows would notice.
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
    "create_agent_state_storage",
    "create_session_storage",
    "create_state_storage",
    "prepare_conversation_database",
    "save_compaction_usage",
]
