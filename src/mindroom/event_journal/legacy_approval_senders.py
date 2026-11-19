"""One-time addition of the sender column to approval cards an earlier release stored."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction
    from .upgrades import SchemaInspector

# LEGACY_COMPAT: Approval cards the router sent, stored without a sender.
# Legacy format: approval_cards rows with no sender_entity column; the router account sent and edited every card.
# Last legacy release: v2026.11.385; replacement: the next release records in approval_cards.sender_entity the entity
# whose account sends and edits each card.
# Handling: the schema upgrade adds the column, which leaves NULL on every stored card; a NULL sender is the router
# account wherever a card is sent, edited or verified (ApprovalMatrixTransport.sender_bot), and the first startup
# expires each such pending card (ApprovalRecovery.end_cards_without_sender), without an edit where no router runs.
# Coverage: tests/test_legacy_approval_records.py::test_pending_router_card_is_ended_once_at_upgrade,
# tests/test_legacy_approval_records.py::test_grants_survive_upgrade.


def upgrade_card_sender(transaction: Transaction, inspector: SchemaInspector) -> None:
    """Add the sender column to an approval card table that lacks it, inside the schema transaction."""
    if "approval_cards" in inspector.tables() and "sender_entity" not in inspector.columns("approval_cards"):
        transaction.execute("ALTER TABLE approval_cards ADD COLUMN sender_entity TEXT")
