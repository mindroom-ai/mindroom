"""Rewrite the event journal after a Matrix server renamed an agent's account in place.

The journal keys each bot's state by its principal, ``<agent>@<Matrix ID>``, and mirrors Matrix senders and payloads.
After the server rename, every event the server re-delivers carries the new ID.
A stored sender that differs from the re-delivered one fails ingestion with ``IngestionBatchIntegrityError``,
so the journal must hold the new ID everywhere the server now shows it.
"""

from __future__ import annotations

import sqlite3
from functools import partial
from typing import TYPE_CHECKING

from mindroom.agent_rename.matching import contains_user_id, replace_user_id
from mindroom.agent_rename.sqlite_read import quote_identifier, read_only_connection, text_columns
from mindroom.agent_rename.steps import RenameRefusedError, SqliteEdit

if TYPE_CHECKING:
    from pathlib import Path

_PRINCIPAL_COLUMNS = frozenset({"principal_id", "responder_principal_id"})
# Rows for replies MindRoom already sent to Matrix; discarding them would lose track of messages in the rooms.
_SENT_REPLY_TABLES = ("reply_messages", "matrix_delivery_outbox")
# Grants hash the old principal into their scope key, so a renamed grant could never match again.
_GRANT_TABLES = ("approval_grants", "approval_grant_cards")
# A principal's sync consumer and the producer positions its stream reached; they belong to one device's sync stream.
_SYNC_STREAM_TABLES = ("matrix_sync_consumers", "matrix_ingestion_membership")

type _SqliteValue = str | bytes | int | float | None


def journal_account_steps(
    database: Path,
    *,
    agent: str,
    old_user_id: str,
    new_user_id: str,
    discard_new_principal: bool,
    new_device_kept: bool,
) -> tuple[list[SqliteEdit], list[str]]:
    """Return the one edit that moves ``agent``'s journal state from ``old_user_id`` to ``new_user_id``, and reports.

    When the old principal has rows, the edit moves them: it renames the principal, replaces the old ID in every
    text cell, and drops the principal's approval grants. Rows the new principal has as well mean
    MindRoom ran under the new ID before the journal was moved; that refuses unless ``discard_new_principal``
    is set, and always when they include replies already sent.

    The sync stream state, a principal's sync consumer and producer membership positions, must belong to the stream
    the device's store continues. The old principal's never does, so it is dropped. The new principal's stays only
    when ``new_device_kept`` says the stored account keeps the device it uses under the new ID; otherwise the next
    start logs in on a fresh device that binds a fresh stream, and it is dropped as well.

    When the old principal has no rows there is nothing to move, and the new principal's rows are the agent's own
    history: moved by an earlier run that was killed or is repeated, or written since. They are never deleted,
    whatever ``discard_new_principal`` says. Text cells that still hold the old ID, such as the agent's messages
    as other principals saw them, are still rewritten; when there are none, the journal is left alone.

    The edit checks the old principal again in its transaction and refuses if a rewritten row would collide.
    A journal that was never created needs no edit.
    """
    if not database.exists():
        return [], []
    old_principal = f"{agent}@{old_user_id}"
    new_principal = f"{agent}@{new_user_id}"
    try:
        with read_only_connection(database) as connection:
            columns = text_columns(connection)
            old_rows = _principal_row_counts(connection, columns, old_principal)
            new_rows = _principal_row_counts(connection, columns, new_principal)
            old_cells = [] if old_rows else _cells_holding(connection, columns, old_user_id)
    except (sqlite3.Error, OSError) as exc:
        msg = f"Cannot read the event journal {database}: {exc}"
        raise RenameRefusedError(msg) from exc
    if old_rows:
        _refuse_new_principal_rows(
            old_rows,
            new_rows,
            old_principal=old_principal,
            new_principal=new_principal,
            discard=discard_new_principal,
        )
        kept = {table: count for table, count in new_rows.items() if new_device_kept and table in _SYNC_STREAM_TABLES}
        discarded = {table: count for table, count in new_rows.items() if table not in kept}
        deletion = f"delete the rows already under {new_principal} ({_format_counts(discarded)}), " if discarded else ""
        stream = (
            f"drop the sync stream state of {old_principal} and keep that of {new_principal} "
            f"({_format_counts(kept)}), whose device continues that stream"
            if kept
            else "drop the principal's sync stream state, so the next start binds a fresh stream"
        )
        description = (
            f"Rewrite the event journal {database}: {deletion}rename principal {old_principal} to {new_principal}, "
            f"replace {old_user_id} with {new_user_id} in every text cell, "
            f"{stream}, and drop the principal's approval grants"
        )
    elif old_cells:
        description = (
            f"Rewrite the event journal {database}: replace {old_user_id} with {new_user_id} in {', '.join(old_cells)}; "
            f"{old_principal} has no rows, so the rows of {new_principal} stay"
        )
    else:
        kept = f"; its rows for {new_principal} ({_format_counts(new_rows)}) are kept" if new_rows else ""
        return [], [f"The event journal {database} has no rows for {old_principal}, so it stays as it is{kept}."]
    edit = partial(
        _rewrite,
        old_user_id=old_user_id,
        new_user_id=new_user_id,
        old_principal=old_principal,
        new_principal=new_principal,
        move_principal=bool(old_rows),
        discard_new_principal=discard_new_principal,
        new_device_kept=new_device_kept,
    )
    return [SqliteEdit(database=database, edit=edit, description=description)], []


def other_principal_user_ids(database: Path, *, agent: str, user_id: str) -> dict[str, str]:
    """Return the IDs other than ``user_id`` that ``agent``'s journal principals name, with their row counts.

    The journal keys ``agent``'s rows by ``<agent>@<Matrix ID>``, so rows under another ID mean the journal still
    holds state from before the stored account took ``user_id``.
    A journal that was never created holds none.
    """
    if not database.exists():
        return {}
    prefix = f"{agent}@"
    try:
        with read_only_connection(database) as connection:
            columns = text_columns(connection)
            principals: set[str] = set()
            for table, names in columns.items():
                for column in names:
                    if column not in _PRINCIPAL_COLUMNS:
                        continue
                    quoted = quote_identifier(column)
                    # substr instead of LIKE: `_` in an agent name is a LIKE wildcard.
                    query = f"SELECT DISTINCT {quoted} FROM {quote_identifier(table)} WHERE substr({quoted}, 1, ?) = ?"  # noqa: S608 - quoted schema identifiers
                    principals.update(principal for (principal,) in connection.execute(query, (len(prefix), prefix)))
            return {
                principal.removeprefix(prefix): _format_counts(_principal_row_counts(connection, columns, principal))
                for principal in sorted(principals)
                if principal != f"{prefix}{user_id}"
            }
    except (sqlite3.Error, OSError) as exc:
        msg = f"Cannot read the event journal {database}: {exc}"
        raise RenameRefusedError(msg) from exc


def _sql_contains_user_id(value: _SqliteValue, user_id: str) -> bool:
    return isinstance(value, str) and contains_user_id(value, user_id)


def _sql_replace_user_id(value: _SqliteValue, old_user_id: str, new_user_id: str) -> _SqliteValue:
    return replace_user_id(value, old_user_id, new_user_id) if isinstance(value, str) else value


def _holds_user_id(quoted_column: str) -> str:
    """Return a condition true when the column holds ``:old`` as a whole user ID.

    ``instr`` runs first so the Python matcher only sees cells that contain the ID's text at all.
    """
    return f"instr({quoted_column}, :old) > 0 AND contains_user_id({quoted_column}, :old)"


def _principal_match(columns: tuple[str, ...]) -> str:
    """Return a condition matching rows whose principal columns name ``:principal``, or ``""`` if there are none."""
    return " OR ".join(f"{quote_identifier(column)} = :principal" for column in columns if column in _PRINCIPAL_COLUMNS)


def _principal_row_counts(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    principal: str,
) -> dict[str, int]:
    """Count, per table, the rows that name ``principal`` in a principal column."""
    counts: dict[str, int] = {}
    for table, names in columns.items():
        match = _principal_match(names)
        if not match:
            continue
        query = f"SELECT count(*) FROM {quote_identifier(table)} WHERE {match}"  # noqa: S608 - quoted schema identifiers
        (count,) = connection.execute(query, {"principal": principal}).fetchone()
        if count:
            counts[table] = count
    return counts


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{table}: {count}" for table, count in sorted(counts.items()))


def _refuse_new_principal_rows(
    old_counts: dict[str, int],
    new_counts: dict[str, int],
    *,
    old_principal: str,
    new_principal: str,
    discard: bool,
) -> None:
    """Refuse when the new principal has rows besides the old one's, unless they may be discarded.

    Only called while the old principal has rows: without them the new principal's rows are the agent's history.
    """
    if not new_counts:
        return
    both = (
        f"The event journal has rows for both {old_principal} ({_format_counts(old_counts)}) "
        f"and {new_principal} ({_format_counts(new_counts)}), "
        "because MindRoom ran under the new ID before the journal was moved"
    )
    if any(table in new_counts for table in _SENT_REPLY_TABLES):
        msg = (
            f"{both}. The {new_principal} rows include replies MindRoom already sent under the new ID, "
            "which --discard-new-principal does not delete."
        )
        raise RenameRefusedError(msg)
    if not discard:
        msg = (
            f"{both}. Rerun with --discard-new-principal to delete the {new_principal} rows "
            f"and move the {old_principal} rows in their place."
        )
        raise RenameRefusedError(msg)


def _register_matchers(connection: sqlite3.Connection) -> None:
    connection.create_function("contains_user_id", 2, _sql_contains_user_id, deterministic=True)
    connection.create_function("replace_user_id", 3, _sql_replace_user_id, deterministic=True)


def _rewrite(
    connection: sqlite3.Connection,
    *,
    old_user_id: str,
    new_user_id: str,
    old_principal: str,
    new_principal: str,
    move_principal: bool,
    discard_new_principal: bool,
    new_device_kept: bool,
) -> None:
    """Move the journal to the new ID inside the caller's transaction; raising rolls everything back.

    Without ``move_principal`` the old principal had no rows when planned, so only text cells change
    and nothing of the new principal is deleted, its sync stream state included.
    """
    _register_matchers(connection)
    columns = text_columns(connection)
    old_rows = _principal_row_counts(connection, columns, old_principal)
    if bool(old_rows) != move_principal:
        # Never decide about deleting the new principal's rows on a journal the plan did not see.
        change = "no longer has" if move_principal else "now has"
        msg = (
            f"The event journal {change} rows for {old_principal}, unlike when the plan was made. "
            "The event journal was left unchanged; run the command again to plan anew."
        )
        raise RenameRefusedError(msg)
    if move_principal:
        new_rows = _principal_row_counts(connection, columns, new_principal)
        _refuse_new_principal_rows(
            old_rows,
            new_rows,
            old_principal=old_principal,
            new_principal=new_principal,
            discard=discard_new_principal,
        )
        # The new device's store continues the new principal's stream, so that stream's state must stay.
        kept = _SYNC_STREAM_TABLES if new_device_kept else ()
        discarded = tuple(table for table in new_rows if table not in kept)
        _delete_principal_rows(connection, columns, tables=discarded, principal=new_principal)
        # No device continues the old login's stream; a fresh stream starts every room at membership epoch 0.
        _delete_principal_rows(connection, columns, tables=_SYNC_STREAM_TABLES, principal=old_principal)
    _replace_identity(
        connection,
        columns,
        {"old_principal": old_principal, "new_principal": new_principal, "old": old_user_id, "new": new_user_id},
    )
    if "room_member_joins" in columns:
        # Any old-ID row left here collided with a row the bot already recorded for the new ID.
        connection.execute("DELETE FROM room_member_joins WHERE user_id = ?", (old_user_id,))
    if move_principal:
        _delete_principal_rows(connection, columns, tables=_GRANT_TABLES, principal=new_principal)
    _refuse_leftovers(connection, columns, old_user_id=old_user_id, new_user_id=new_user_id)


def _delete_principal_rows(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    *,
    tables: tuple[str, ...],
    principal: str,
) -> None:
    """Delete the rows of ``tables`` that name ``principal`` in a principal column."""
    for table in tables:
        match = _principal_match(columns.get(table, ()))
        if match:
            delete = f"DELETE FROM {quote_identifier(table)} WHERE {match}"  # noqa: S608 - quoted schema identifiers
            connection.execute(delete, {"principal": principal})


def _replace_identity(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    names: dict[str, str],
) -> None:
    """Rename the principal in every principal column, then replace the old ID in every other text column."""
    renames: list[str] = []
    replacements: list[str] = []
    for table, table_columns in columns.items():
        for column in table_columns:
            quoted_table, quoted = quote_identifier(table), quote_identifier(column)
            if column in _PRINCIPAL_COLUMNS:
                renames.append(f"UPDATE {quoted_table} SET {quoted} = :new_principal WHERE {quoted} = :old_principal")  # noqa: S608 - quoted schema identifiers
            else:
                # A row whose rewrite would collide keeps the old ID, and the leftover check refuses it.
                replacements.append(
                    f"UPDATE OR IGNORE {quoted_table} SET {quoted} = replace_user_id({quoted}, :old, :new) "  # noqa: S608 - quoted schema identifiers
                    f"WHERE {_holds_user_id(quoted)}",
                )
    for statement in (*renames, *replacements):
        connection.execute(statement, names)


def _cells_holding(connection: sqlite3.Connection, columns: dict[str, tuple[str, ...]], user_id: str) -> list[str]:
    """Return ``table.column (count)`` for every text column with cells that hold ``user_id`` as a whole user ID."""
    _register_matchers(connection)
    cells: list[str] = []
    for table, table_columns in columns.items():
        if not table_columns:
            continue
        sums = ", ".join(f"sum({_holds_user_id(quote_identifier(column))})" for column in table_columns)
        counts = connection.execute(f"SELECT {sums} FROM {quote_identifier(table)}", {"old": user_id}).fetchone()  # noqa: S608 - quoted schema identifiers
        cells.extend(
            f"{table}.{column} ({count})" for column, count in zip(table_columns, counts, strict=True) if count
        )
    return cells


def _refuse_leftovers(
    connection: sqlite3.Connection,
    columns: dict[str, tuple[str, ...]],
    *,
    old_user_id: str,
    new_user_id: str,
) -> None:
    """Refuse when any text cell still holds the old ID as a whole user ID, naming each table and column."""
    leftovers = _cells_holding(connection, columns, old_user_id)
    if leftovers:
        msg = (
            f"Rewriting the event journal would leave {old_user_id} in {', '.join(leftovers)}, "
            f"most likely in rows that would collide with rows already holding {new_user_id}. "
            "The event journal was left unchanged."
        )
        raise RenameRefusedError(msg)
