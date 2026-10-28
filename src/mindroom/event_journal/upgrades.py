"""The journal's schema upgrades, in the order every backend runs them.

Each backend opens one schema transaction, runs every upgrade below against what
the database holds, and then creates the current schema. An upgrade inspects the
tables and columns it converts and does nothing once they are current, so a fresh
or already-upgraded journal skips it. A new upgrade is one more entry here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .legacy_approval_failures import upgrade_failure_kind
from .legacy_approval_records import upgrade_approval_records
from .legacy_response_attempts import upgrade_continuation_identity
from .legacy_schema import upgrade_hydration_walk_version, upgrade_legacy_journal, upgrade_outbox_reply_rows
from .legacy_turn_rows import upgrade_turn_records_to_rows

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
    upgrade_outbox_reply_rows,
    upgrade_continuation_identity,
    upgrade_failure_kind,
    upgrade_approval_records,
    upgrade_turn_records_to_rows,
)


def upgrade_schema(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Run every journal upgrade in order, inside the transaction that then creates the current schema."""
    for upgrade in _JOURNAL_UPGRADES:
        upgrade(transaction, inspector)
