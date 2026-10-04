"""Reply messages that hold their conversation's outstanding background work between turns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Row, Transaction


@dataclass(frozen=True, slots=True)
class SavedHeldReply:
    """One saved hold: its JSON snapshot, the save that wrote it, and the save a wake was last admitted for."""

    hold_id: str
    hold_json: str
    # Unique to each save, even of a hold released and saved again, so nothing can mistake a later save for it.
    generation: str
    woken_generation: str | None


def _saved(row: Row) -> SavedHeldReply:
    return SavedHeldReply(
        str(row["hold_id"]),
        str(row["hold_json"]),
        str(row["generation"]),
        None if row["woken_generation"] is None else str(row["woken_generation"]),
    )


_COLUMNS = "hold_id, hold_json, generation, woken_generation"


def load(transaction: Transaction, hold_id: str) -> SavedHeldReply | None:
    """Return one hold, or ``None`` when no reply holds that work."""
    row = transaction.fetchone(f"SELECT {_COLUMNS} FROM held_replies WHERE hold_id = ?", (hold_id,))  # noqa: S608
    return None if row is None else _saved(row)


def load_for_message(transaction: Transaction, recipient: str, message_event_id: str) -> SavedHeldReply | None:
    """Return the hold one recipient's reply message carries."""
    row = transaction.fetchone(
        f"SELECT {_COLUMNS} FROM held_replies WHERE recipient = ? AND message_event_id = ?",  # noqa: S608
        (recipient, message_event_id),
    )
    return None if row is None else _saved(row)


def load_all(transaction: Transaction) -> tuple[SavedHeldReply, ...]:
    """Return every hold in ID order."""
    rows = transaction.fetchall(f"SELECT {_COLUMNS} FROM held_replies ORDER BY hold_id/*bytes*/")  # noqa: S608
    return tuple(_saved(row) for row in rows)


def save(
    transaction: Transaction,
    *,
    hold_id: str,
    recipient: str,
    message_event_id: str | None,
    hold_json: str,
    generation: str,
) -> tuple[SavedHeldReply | None, SavedHeldReply]:
    """Make a reply the holder of its work as a new generation, returning the hold it replaced and the new one."""
    previous = load(transaction, hold_id)
    row = transaction.fetchone(
        f"""
        INSERT INTO held_replies (hold_id, recipient, message_event_id, hold_json, generation, woken_generation)
        VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT (hold_id) DO UPDATE SET
            recipient = excluded.recipient,
            message_event_id = excluded.message_event_id,
            hold_json = excluded.hold_json,
            generation = excluded.generation,
            woken_generation = NULL
        RETURNING {_COLUMNS}
        """,  # noqa: S608
        (hold_id, recipient, message_event_id, hold_json, generation),
    )
    assert row is not None
    return previous, _saved(row)


def resave(
    transaction: Transaction,
    hold_id: str,
    *,
    generation: str,
    hold_json: str,
    new_generation: str,
) -> SavedHeldReply | None:
    """Save a hold again as a new generation, only while ``generation`` still holds the work."""
    row = transaction.fetchone(
        f"""
        UPDATE held_replies SET hold_json = ?, generation = ?, woken_generation = NULL
        WHERE hold_id = ? AND generation = ?
        RETURNING {_COLUMNS}
        """,  # noqa: S608
        (hold_json, new_generation, hold_id, generation),
    )
    return None if row is None else _saved(row)


def delete(transaction: Transaction, hold_id: str, *, generation: str | None = None) -> SavedHeldReply | None:
    """Release a hold, only that ``generation`` of it when one is given, returning the hold that was released."""
    if generation is None:
        row = transaction.fetchone(
            f"DELETE FROM held_replies WHERE hold_id = ? RETURNING {_COLUMNS}",  # noqa: S608
            (hold_id,),
        )
    else:
        row = transaction.fetchone(
            f"DELETE FROM held_replies WHERE hold_id = ? AND generation = ? RETURNING {_COLUMNS}",  # noqa: S608
            (hold_id, generation),
        )
    return None if row is None else _saved(row)


def mark_woken(transaction: Transaction, hold_id: str, generation: str) -> None:
    """Record that a wake was admitted for this generation of a hold, unless another save replaced it."""
    transaction.execute(
        "UPDATE held_replies SET woken_generation = ? WHERE hold_id = ? AND generation = ?",
        (generation, hold_id, generation),
    )
