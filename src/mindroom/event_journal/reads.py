"""Bounded conversation reads.

Every read takes a limit. There is no API that materializes a whole room,
because the only thing standing between a busy room and an unbounded query is
whether such a call exists to be made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .identity import decode_thread_id, encode_thread_id
from .models import (
    ConversationCursor,
    ConversationPage,
    RefreshRequest,
    VisibleMessage,
)
from .projection import decode_content

if TYPE_CHECKING:
    from .backend import Row, Transaction

_PAGE_COLUMNS = """
    logical_event_id, room_id, thread_id, sender, created_ts,
    revision_event_id, revision_ts, content_json, refresh_token, membership_epoch
"""


def read_conversation(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    limit: int,
    before: ConversationCursor | None = None,
) -> ConversationPage:
    """Return one bounded page of a conversation, oldest first.

    A message whose visible revision was redacted is reported in
    ``refresh_pending`` and is absent from ``messages``. There is no read that
    returns it: the row's body was cleared in the same transaction that
    admitted the redaction, so no caller can serve deleted content, whether or
    not it is willing to wait for the refetch.
    """
    if limit <= 0:
        msg = "A conversation read requires a positive limit"
        raise ValueError(msg)
    rows = _page_rows(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        limit=limit,
        before=before,
    )
    messages: list[VisibleMessage] = []
    refresh_pending: list[RefreshRequest] = []
    for row in rows:
        if row["content_json"] is None:
            refresh_pending.append(_refresh_request(row))
            continue
        messages.append(_visible_message(row))
    next_cursor = (
        ConversationCursor(
            created_ts=int(rows[-1]["created_ts"]),
            logical_event_id=rows[-1]["logical_event_id"],
        )
        if len(rows) == limit
        else None
    )
    return ConversationPage(
        messages=tuple(reversed(messages)),
        refresh_pending=tuple(refresh_pending),
        next_cursor=next_cursor,
    )


def _page_rows(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    limit: int,
    before: ConversationCursor | None,
) -> tuple[Row, ...]:
    """Return one page's rows, newest first.

    A thread's root message carries no thread relation of its own — it becomes
    a root only when someone replies to it — so it is stored in the room
    conversation. Reading a thread therefore merges its replies with that one
    extra row, which is a primary-key lookup rather than a scan.
    """
    cursor_clause = "" if before is None else " AND (created_ts < ? OR (created_ts = ? AND logical_event_id < ?))"
    cursor_params: tuple[object, ...] = (
        () if before is None else (before.created_ts, before.created_ts, before.logical_event_id)
    )
    rows = list(
        transaction.fetchall(
            f"""
            SELECT {_PAGE_COLUMNS} FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND thread_id = ?{cursor_clause}
            ORDER BY created_ts DESC, logical_event_id DESC
            LIMIT ?
            """,  # noqa: S608 - a fixed column list and a fixed clause, not input
            (principal_id, room_id, encode_thread_id(thread_id), *cursor_params, limit),
        ),
    )
    if thread_id is not None:
        root = transaction.fetchone(
            f"""
            SELECT {_PAGE_COLUMNS} FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?{cursor_clause}
            """,  # noqa: S608 - a fixed column list and a fixed clause, not input
            (principal_id, room_id, thread_id, *cursor_params),
        )
        if root is not None and all(row["logical_event_id"] != thread_id for row in rows):
            rows.append(root)
            rows.sort(key=lambda row: (int(row["created_ts"]), row["logical_event_id"]), reverse=True)
            del rows[limit:]
    return tuple(rows)


def latest_visible_event_id(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str,
) -> str | None:
    """Return the newest visible event in one thread, or nothing if it is empty.

    The revision, not the logical message: the caller is building an
    ``m.in_reply_to`` fallback for clients that do not understand threads, and
    what those render is the event that is actually in the room.

    A message awaiting a refetch still answers. Its body is withheld from every
    read, but its identity is not in doubt, and pointing a reply at it is
    correct whether or not its text is currently servable.
    """
    row = transaction.fetchone(
        """
        SELECT revision_event_id FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
        ORDER BY created_ts DESC, logical_event_id DESC
        LIMIT 1
        """,
        (principal_id, room_id, encode_thread_id(thread_id)),
    )
    return None if row is None else str(row["revision_event_id"])


def pending_refreshes(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    limit: int,
) -> tuple[RefreshRequest, ...]:
    """Return logical messages in one conversation that owe a point refetch.

    A thread's root is stored in the room conversation, so a read of the thread
    merges it in. This has to merge it the same way: a root whose visible
    revision was redacted is missing from the thread read, and a repair pass
    that could not see it would leave that read permanently incomplete.
    """
    rows = list(
        transaction.fetchall(
            f"""
            SELECT {_PAGE_COLUMNS} FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND thread_id = ? AND refresh_token IS NOT NULL
            ORDER BY created_ts DESC, logical_event_id DESC
            LIMIT ?
            """,  # noqa: S608 - a fixed column list, not interpolated input
            (principal_id, room_id, encode_thread_id(thread_id), limit),
        ),
    )
    if thread_id is not None:
        root = transaction.fetchone(
            f"""
            SELECT {_PAGE_COLUMNS} FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND logical_event_id = ? AND refresh_token IS NOT NULL
            """,  # noqa: S608 - a fixed column list, not interpolated input
            (principal_id, room_id, thread_id),
        )
        if root is not None and all(row["logical_event_id"] != thread_id for row in rows):
            rows.append(root)
            rows.sort(key=lambda row: (int(row["created_ts"]), row["logical_event_id"]), reverse=True)
            del rows[limit:]
    return tuple(_refresh_request(row) for row in rows)


def conversation_is_hydrated(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> bool:
    """Return whether this conversation was hydrated under the current membership."""
    row = transaction.fetchone(
        """
        SELECT hydration.membership_epoch AS hydrated_epoch,
               COALESCE(membership.membership_epoch, 0) AS current_epoch
        FROM conversation_hydration AS hydration
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = hydration.principal_id
         AND membership.room_id = hydration.room_id
        WHERE hydration.principal_id = ? AND hydration.room_id = ? AND hydration.thread_id = ?
        """,
        (principal_id, room_id, encode_thread_id(thread_id)),
    )
    return row is not None and int(row["hydrated_epoch"]) == int(row["current_epoch"])


def mark_conversation_hydrated(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    expected_membership_epoch: int,
) -> bool:
    """Record a completed hydration, unless membership moved under it.

    Hydration and this record commit together, so a conversation is never
    marked hydrated against events that a rejoin has already invalidated.
    """
    row = transaction.fetchone(
        """
        SELECT COALESCE(membership_epoch, 0) AS epoch FROM room_membership
        WHERE principal_id = ? AND room_id = ?
        """,
        (principal_id, room_id),
    )
    current_epoch = 0 if row is None else int(row["epoch"])
    if current_epoch != expected_membership_epoch:
        return False
    transaction.execute(
        """
        INSERT INTO conversation_hydration (principal_id, room_id, thread_id, membership_epoch)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, thread_id) DO UPDATE SET
            membership_epoch = excluded.membership_epoch
        """,
        (principal_id, room_id, encode_thread_id(thread_id), current_epoch),
    )
    return True


def _visible_message(row: Row) -> VisibleMessage:
    return VisibleMessage(
        logical_event_id=row["logical_event_id"],
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        sender=row["sender"],
        created_ts=int(row["created_ts"]),
        revision_event_id=row["revision_event_id"],
        revision_ts=int(row["revision_ts"]),
        content=decode_content(row["content_json"]),
    )


def _refresh_request(row: Row) -> RefreshRequest:
    return RefreshRequest(
        room_id=row["room_id"],
        thread_id=decode_thread_id(row["thread_id"]),
        logical_event_id=row["logical_event_id"],
        refresh_token=int(row["refresh_token"]),
        membership_epoch=int(row["membership_epoch"]),
    )
