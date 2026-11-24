"""What every reader of a conversation database shows of it, as JSON-safe values.

A database's readers are the store's loads and reads, the history views the storage oracle observes, the background
readers' list of updated sessions, and the usage report's rows. Each session is read under the scope that owns the
database, as the runtime reads it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import fields, is_dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.session_storage.schema import quote_identifier
from mindroom.usage.stats_storage import UsageStorageSource, iter_usage_storage_rows
from mindroom.usage.storage import SYSTEM_USAGE_ENTITY
from tests.storage_oracle.observe import observe

if TYPE_CHECKING:
    from typing import Literal

    from mindroom.history.types import HistoryScope
    from mindroom.session_storage.store import ConversationStore


type _UsageScope = Literal["shared_agent", "team", "system"]


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value) if field.name != "source"}
    if isinstance(value, dict | MappingProxyType):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def _usage_rows(store: ConversationStore, owner: HistoryScope) -> list[object]:
    """Return the usage report's rows of the database, with and without a start time.

    Each read is listed under both read modes the release had, whose rows a read without one now gives alike.
    """
    scopes: dict[str, _UsageScope] = {"agent": "shared_agent", "team": "team"}
    scope = "system" if owner.scope_id == SYSTEM_USAGE_ENTITY else scopes[owner.kind]
    source = UsageStorageSource(
        path=store.path,
        path_label=store.path.name,
        scope=scope,
        source_agent_id=owner.scope_id if scope == "shared_agent" else None,
        allowed_agent_ids=frozenset({owner.scope_id}),
        requester_isolated=False,
    )
    return [
        {"mode": mode, "since": since, "row": _plain(row)}
        for mode in ("runs", "both")
        for since in (None, 0)
        for row in iter_usage_storage_rows(source, since=since)
    ]


def _updated_sessions(store: ConversationStore, owner: HistoryScope) -> object:
    """Return the sessions the background readers of an agent's conversations see updated."""
    if owner.kind != "agent":
        return None
    return store.list_updated_sessions(since=0)


def observe_database(store: ConversationStore, owner: HistoryScope) -> dict[str, object]:
    """Return what every reader shows of ``store``, whose database ``owner`` owns."""
    with closing(sqlite3.connect(store.path)) as connection:
        session_ids = [
            session_id
            for (session_id,) in connection.execute(
                f"SELECT session_id FROM {quote_identifier(store.names.sessions)} ORDER BY session_id",  # noqa: S608
            )
        ]
    sessions = {}
    for session_id in session_ids:
        run_ids = [run.run_id for run in store.list_runs(session_id)]
        sessions[session_id] = {
            "observed": observe(store, session_id, owner, run_ids=run_ids),
            "metadata": store.read_session_metadata(session_id),
            "runs_by_id": {run_id: _plain(store.get_run(run_id).to_dict()) for run_id in run_ids},  # type: ignore[union-attr]
        }
    shown = {
        "sessions": sessions,
        "usage_rows": _usage_rows(store, owner),
        "updated_sessions": _updated_sessions(store, owner),
    }
    return json.loads(json.dumps(shown, sort_keys=True))
