"""Open the conversation store of an agent, a team scope, or the system usage ledger, converting the database first."""

from __future__ import annotations

import time
from contextlib import ExitStack, closing
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.history.types import HistoryScope
from mindroom.llm.messages import ModelProvenance
from mindroom.llm.usage import HelperUsage, RequestUsage, RunMetrics
from mindroom.runtime_resolution import record_private_instance_owner, resolve_agent_storage
from mindroom.session_storage.legacy_agno3_conversion import convert_conversation_database
from mindroom.session_storage.legacy_session_columns import upgrade_layout
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import (
    LOG_VERSION,
    TableNames,
    create_log_schema,
    is_current_layout,
    layout_version,
    table_names,
)
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


def create_state_storage(storage_name: str, state_root: Path, owner: HistoryScope) -> ConversationStore:
    """Open the conversation database ``<state_root>/sessions/<storage_name>.db`` that ``owner`` owns, converted first.

    ``owner`` is the agent, team scope, or system usage ledger every session in it belongs to. Under the recovery
    lock, in order: a database missing required session columns is archived, a database in an older layout converts,
    a new one gets every table with ``owner`` recorded, and only then does the conversation store open it.
    """
    names = TableNames(storage_name)
    db_path = names.database_path(state_root)
    with session_storage_preflight(state_root, names, timeout_seconds=BUSY_TIMEOUT_SECONDS):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        prepare_conversation_database(db_path, names, owner)
        return ConversationStore(db_path)


def create_agent_state_storage(agent_name: str, state_root: Path) -> ConversationStore:
    """Open agent ``agent_name``'s conversation database as ``create_state_storage`` does."""
    return create_state_storage(agent_name, state_root, HistoryScope(kind="agent", scope_id=agent_name))


def prepare_conversation_database(path: Path, names: TableNames, owner: HistoryScope) -> None:
    """Bring the database at ``path``, which ``owner`` owns, to the current layout before a store opens it.

    The connection follows no link to the file (``connect_by_name``). A database in the current layout costs reading
    its layout version, and converts once when that is an older one (``upgrade_layout``); a new or empty one gets every
    table, and one that holds older tables is handed to the one-way conversion.
    """
    with ExitStack() as descriptors, closing(connect_by_name(path)) as connection:
        if (version := layout_version(connection, names)) is not None:
            if version < LOG_VERSION:
                upgrade_layout(connection, path, names, descriptors, owner)
            return
        if not table_names(connection):
            _create_tables(connection, names, owner)
        if not is_current_layout(connection, names):
            convert_conversation_database(connection, path, names, descriptors, owner=owner)


def _create_tables(connection: sqlite3.Connection, names: TableNames, owner: HistoryScope) -> None:
    # Asked again under the lock: another opener may have created or converted the file meanwhile.
    with begun(connection, write=True):
        if not table_names(connection):
            create_log_schema(connection, names, owner)


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
    """Persist one summary response's returned counters, with the model that answered it, apart from the conversation."""
    created_at = time.time()
    summary = HelperUsage(
        run_id=str(uuid4()),
        model=model.id,
        provider=model.provider_name,
        metrics=RunMetrics(tokens=usage.tokens),
        requests=(
            RequestUsage(
                created_at=created_at,
                metrics=usage,
                model=ModelProvenance(model_id=model.id, provider=model.provider_name),
            ),
        ),
        created_at=created_at,
    )
    store.save_independent_usage(
        session_id=session_id,
        usage_id=f"compaction:{summary.run_id}",
        kind="compaction_summary",
        requester_id=requester_id,
        initial_session=initial_session,
        usage=summary,
    )


__all__ = [
    "create_agent_state_storage",
    "create_session_storage",
    "create_state_storage",
    "prepare_conversation_database",
    "save_compaction_usage",
]
