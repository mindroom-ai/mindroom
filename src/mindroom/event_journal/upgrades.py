"""The journal's schema upgrades, in the order every backend runs them.

Each backend opens one schema transaction, in which every upgrade below runs
against what the database holds, then the current schema is created, and then the
conversions that fill it from tables an earlier release left run. An upgrade
inspects the tables and columns it converts and does nothing once they are
current, so a fresh or already-upgraded journal skips it. A new upgrade is one more
entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .legacy_approval_failures import upgrade_failure_kind
from .legacy_approval_records import upgrade_approval_records
from .legacy_response_attempts import upgrade_continuation_identity
from .legacy_schema import (
    upgrade_content_generation,
    upgrade_hydration_walk_version,
    upgrade_legacy_journal,
    upgrade_outbox_reply_rows,
)
from .legacy_turn_rows import drop_dense_turn_source_indexes, drop_own_index, upgrade_turn_records_to_rows
from .schema import FINGERPRINT_INDEX, create_statement, schema_statements

if TYPE_CHECKING:
    from collections.abc import Callable

    from .backend import Transaction
    from .schema import SchemaDialect


class SchemaInspector(Protocol):
    """What one backend's schema transaction currently holds."""

    @property
    def dialect(self) -> SchemaDialect:
        """Return the spellings of this backend."""
        ...

    def tables(self) -> frozenset[str]:
        """Return the names of every table."""
        ...

    def columns(self, table: str) -> frozenset[str]:
        """Return the column names of one table, empty when it does not exist."""
        ...


type _JournalUpgrade = Callable[[Transaction, SchemaInspector], None]

_JOURNAL_UPGRADES: tuple[_JournalUpgrade, ...] = (
    upgrade_legacy_journal,
    upgrade_hydration_walk_version,
    upgrade_content_generation,
    upgrade_outbox_reply_rows,
    upgrade_continuation_identity,
    upgrade_failure_kind,
    upgrade_approval_records,
    drop_dense_turn_source_indexes,
)


def upgrade_schema(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Run every journal upgrade in order, create the current schema, and fill it from the tables they kept."""
    numbered = "content_generation" in inspector.columns("visible_messages")
    for upgrade in _JOURNAL_UPGRADES:
        upgrade(transaction, inspector)
    staged = create_statement(FINGERPRINT_INDEX, inspector.dialect)
    for statement in schema_statements(inspector.dialect):
        if statement != staged:
            transaction.execute(statement)
    # Once per journal: the count starts at the highest number a body holds. That is 0 unless bodies were numbered
    # before this open, so only then is every message read for it, under the locks of this transaction.
    if transaction.fetchone("SELECT latest FROM content_generations") is None:
        highest = "SELECT COALESCE(MAX(content_generation), 0) FROM visible_messages" if numbered else "SELECT 0"
        transaction.execute(f"INSERT INTO content_generations {highest}")
    # A redaction's effects read membership, reply, and continuation tables that only the current schema has.
    upgrade_turn_records_to_rows(transaction, inspector)


def build_staged_indexes(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Build the fingerprint index in a transaction after the schema's, then drop by name the one it replaces.

    The schema's transaction holds the locks of its upgrades, on PostgreSQL one that stops readers; the build stops
    only writers. The drop, which on PostgreSQL briefly stops readers too, runs on every open, so a journal an older
    release opened in between does not keep both.
    """
    transaction.execute(create_statement(FINGERPRINT_INDEX, inspector.dialect))
    drop_own_index(transaction, inspector, "visible_messages_fingerprint")
