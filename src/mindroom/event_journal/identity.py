"""Leaf identity helpers shared by every event-journal boundary.

These are the encode/decode rules that primary keys depend on, kept free of
storage and Matrix imports so both backends and the pure reducer can use them.
"""

from __future__ import annotations

import uuid

_UNTHREADED_STORAGE_VALUE = ""

# Stable namespace for deterministic Matrix transaction IDs. Changing it would
# make every unacknowledged delivery retry look like a new message to the
# homeserver, so it is frozen.
_TRANSACTION_NAMESPACE = uuid.UUID("6f2a0e1c-6a7e-5f39-9a5b-2c0d3e4f5a6b")


def encode_thread_id(thread_id: str | None) -> str:
    """Return the single canonical storage value for a conversation thread.

    Typed APIs represent an unthreaded conversation as ``None``, but durable
    tables store ``NOT NULL`` text so primary keys and uniqueness constraints
    never depend on nullable equality, which SQLite and PostgreSQL disagree
    about.
    """
    if thread_id is None:
        return _UNTHREADED_STORAGE_VALUE
    if thread_id == _UNTHREADED_STORAGE_VALUE:
        msg = "The empty string is reserved for the unthreaded conversation"
        raise ValueError(msg)
    return thread_id


def decode_thread_id(stored_thread_id: str) -> str | None:
    """Return the typed thread identity for one stored value."""
    return None if stored_thread_id == _UNTHREADED_STORAGE_VALUE else stored_thread_id


def delivery_transaction_id(principal_id: str, turn_id: str, stage: str) -> str:
    """Return the deterministic Matrix transaction ID for one delivery stage.

    Derived rather than random so that a retry after a crash reuses the exact
    transaction the homeserver may already have accepted, which is what makes
    redelivery idempotent.

    Deliberately not varied by membership epoch. A rejoin does not undo a send:
    if the homeserver accepted the message it is still in the room, so the only
    convergent thing a retry can do is present the same transaction again and
    collapse back onto the same event. Deriving a fresh identity per epoch
    would post the answer a second time instead.
    """
    if not principal_id or not turn_id or not stage:
        msg = "A delivery transaction requires a principal, turn, and stage"
        raise ValueError(msg)
    name = f"{principal_id}:{turn_id}:{stage}"
    return f"mindroom-{uuid.uuid5(_TRANSACTION_NAMESPACE, name)}"
