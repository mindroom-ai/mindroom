"""Agent session rows that name an owner other than their agent, adopted once as layout version 3."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.session_storage.schema import LOG_VERSION
from mindroom.session_storage.store import begun
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.schema import TableNames


# LEGACY_COMPAT: Agent session rows naming the id Agno derived from the agent's display name.
# Legacy format: In agent `<name>`'s conversation database, a `session_type = 'agent'` row whose `agent_id` is not
# `<name>`. MindRoom built each Agno agent without an id, so Agno stored `generate_id_from_name(display_name)`
# (`Code Agent` became `code-agent`); a row keeps the owner its last save wrote, and storage v2 copied rows as it found
# them. A row without an owner, which no release wrote, is selected too.
# Last legacy release: v2026.2.54; replacement: v2026.2.55 passed the agent's name as its id.
# Handling: The agent's store open and the startup sweep set such a row's `agent_id` to `<name>` once: in the
# conversion of an Agno-layout database, or when a storage v2 database moves to layout version 3. The session's owner
# is then its history scope, so the scope's summary replays after a compaction. Runs keep the id they were saved under.
# Coverage: tests/session_storage/test_legacy_agent_session_owners.py.
def adopt_agent_session_owners(connection: sqlite3.Connection, names: TableNames, agent_name: str) -> None:
    """Make every agent session row in agent ``agent_name``'s database name that agent, in the caller's transaction."""
    connection.execute(
        f"UPDATE {quote_identifier(names.sessions)} SET agent_id = ?1 "  # noqa: S608
        "WHERE session_type = 'agent' AND agent_id IS NOT ?1",
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
