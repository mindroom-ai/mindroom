"""Agent session rows, runs, and summaries that name an owner other than their agent, adopted once as version 3."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.session_storage.schema import LOG_VERSION
from mindroom.session_storage.store import begun
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.schema import TableNames


# LEGACY_COMPAT: Agent session rows, runs, and summaries naming the id Agno derived from the agent's display name.
# Legacy format: In agent `<name>`'s conversation database, a `session_type = 'agent'` row whose `agent_id` is not
# `<name>`, a top-level run whose `agent_id` is not `<name>`, and a summary row under the scope the row's old owner
# names. Until v2026.2.55 MindRoom built each Agno agent without an id, so Agno stored
# `generate_id_from_name(display_name)` (`Code Agent` became `code-agent`) on every session it created. A row keeps the
# owner it was created with, because Agno loaded a stored session with its stored owner and saved that back, and storage
# v2 copied the row as it found it; a run keeps the id it was saved under; and storage v2 stored a row's Agno summary
# under the scope of its owner. A row or run without an owner, which no release wrote, is selected too.
# Last legacy release: v2026.10.660, which still saves such rows with their creation owner; replacement: the next
# release, whose layout version 3 makes them name the agent.
# Handling: The agent's store open and the startup sweep set their agent to `<name>` once: in the conversion of an
# Agno-layout database, or when a storage v2 database moves to layout version 3. The session's owner is then its
# history scope: its summary rows move with it, keeping their order and provenance, a later compaction's summary
# replays, and the runs replay as the agent's history again.
# Coverage: tests/session_storage/test_legacy_agent_session_owners.py.
def adopt_agent_session_owners(connection: sqlite3.Connection, names: TableNames, agent_name: str) -> None:
    """Make every agent session, its summary rows, and its top-level runs in agent ``agent_name``'s database name it.

    Runs in the caller's transaction. A summary row moves from the scope of its session's old owner before the session
    names the agent.
    """
    sessions = quote_identifier(names.sessions)
    summaries = quote_identifier(names.log_summaries)
    connection.execute(
        f"UPDATE {summaries} SET scope_key = 'agent:' || ?1 WHERE scope_key = "  # noqa: S608
        f"(SELECT 'agent:' || agent_id FROM {sessions} AS session "
        f"WHERE session.session_id = {summaries}.session_id AND session.session_type = 'agent')",
        (agent_name,),
    )
    connection.execute(
        f"UPDATE {sessions} SET agent_id = ?1 WHERE session_type = 'agent' AND agent_id IS NOT ?1",  # noqa: S608
        (agent_name,),
    )
    connection.execute(
        f"UPDATE {quote_identifier(names.log_runs)} SET scope_key = 'agent:' || ?1, "  # noqa: S608
        "record = json_set(record, '$.agent_id', ?1) "
        "WHERE parent_run_id IS NULL AND scope_key IS NOT 'agent:' || ?1",
        (agent_name,),
    )


def upgrade_storage_v2(connection: sqlite3.Connection, names: TableNames, agent_name: str | None) -> None:
    """Move a database in the current layout from version 2 to ``LOG_VERSION``, adopting owners in an agent's database.

    ``agent_name`` names the agent whose database it is, or is None for a team's or the system usage database, which
    have nothing to adopt. Only an opener that knows whose database it is upgrades it. A database at the current version
    costs one read and takes no write lock; two openers upgrading at once write the same rows.
    """
    if _version(connection, names) >= LOG_VERSION:
        return
    with begun(connection, write=True):
        if agent_name is not None:
            adopt_agent_session_owners(connection, names, agent_name)
        connection.execute(f"UPDATE {quote_identifier(names.log_meta)} SET version = ? WHERE id = 1", (LOG_VERSION,))  # noqa: S608


def _version(connection: sqlite3.Connection, names: TableNames) -> int:
    (version,) = connection.execute(f"SELECT version FROM {quote_identifier(names.log_meta)} WHERE id = 1").fetchone()  # noqa: S608
    return version


__all__ = [
    "adopt_agent_session_owners",
    "upgrade_storage_v2",
]
