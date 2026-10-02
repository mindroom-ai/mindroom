"""Bounded conversation reads.

Every read takes a limit. There is no API that materializes a whole room,
because the only thing standing between a busy room and an unbounded query is
whether such a call exists to be made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .identity import decode_thread_id, encode_thread_id
from .membership_state import claim_membership_epoch
from .models import (
    ConversationCursor,
    ConversationPage,
    HydrationCoverage,
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
# Order and stored size only, so a page is sized before any content is loaded.
# Both backends read a text value's byte length without loading the value;
# SQLite before 3.43 has no ``octet_length``, and the backend supplies one.
_PAGE_SIZE_COLUMNS = "logical_event_id, created_ts, octet_length(content_json) AS content_bytes"

# How much stored content one page loads, newest message first. A resolved
# long-text sidecar can be megabytes, and anyone who can post can make every
# message in a thread name one, so a row limit alone does not bound a read.
_PAGE_CONTENT_BUDGET_BYTES = 16 * 1024 * 1024

# What the loaded rows of one page may decode to, estimated before decoding.
# Prose decodes to about its stored size, but every JSON value becomes a Python
# object: a string or number of a few bytes takes 35 to 55 bytes once decoded,
# an array about 70 and an object about 190, so a list of short values decodes
# to about 10 times its size and nested empty containers to over 40. Weighing
# every structural array, object and separator keeps such shapes near their
# estimate. Measured worst: a dict just past a hash table resize with
# one-character non-ASCII keys and values holds about 1.25 times it, and a page
# of those peaks near 1.35 times this bound; a list of one-character non-ASCII
# strings holds about 1.05 times it. Meanwhile 16 MiB of prose and tool traces,
# including spaced JSON text inside their strings as tool previews write it,
# stays under this bound.
_PAGE_DECODED_BUDGET_BYTES = 64 * 1024 * 1024
_DECODED_BYTES_PER_ARRAY = 96
_DECODED_BYTES_PER_OBJECT = 192
_DECODED_BYTES_PER_SEPARATOR = 56

# Everything older than one page's last row, spelled as a row value.
#
# The disjunction this replaces -- `created_ts < ? OR (created_ts = ? AND
# logical_event_id < ?)` -- selects exactly the same rows and is not a bound.
# Neither backend can position an index on it, so every page re-entered the
# conversation at its tip and walked forward through everything newer than the
# cursor before returning anything, making a full walk quadratic in the
# conversation. The plan still read SEARCH and the index was still covering,
# which is how it went unnoticed: the query-plan test rejects `SCAN` and
# `TEMP B-TREE`, and this was neither.
#
# Measured on this schema, SQLite 3.53.1, walking the whole conversation 500
# messages at a time at load average 13-14: 0.75 s against 0.037 s at 100,000
# messages, and 77.1 s against 0.38 s at 1,000,000 -- the export ceiling. The
# growth is the point rather than the ratio: the disjunction costs 102.9x more
# for the last 10x of messages where the row value costs 10.1x.
#
# PostgreSQL degrades further rather than less. It answers the disjunction with
# a BitmapOr feeding a Sort, giving up the index ordering as well as the bound,
# and takes the row value as `Index Only Scan Backward` with the cursor in its
# Index Cond.
#
# Shared with the query-plan test rather than copied into it, because a copy
# could go on proving that a seek is possible while this drifted back to a
# spelling that does not use one.
_CONVERSATION_CURSOR_CLAUSE = " AND (created_ts, logical_event_id) < (?, ?)"


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

    A page also ends before ``limit`` once its stored content passes a fixed
    budget, and then carries a cursor exactly as a full page does. Its rows are
    sized first and only the ones that fit are loaded.

    On PostgreSQL the size and content queries can see different snapshots,
    so a write committed between them can carry a page past its budget by the
    rows that write changed. That overshoot is accepted rather than folding
    the thread root's separate lookup into one windowed statement.
    """
    if limit <= 0:
        msg = "A conversation read requires a positive limit"
        raise ValueError(msg)
    sizes = _page_rows(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        limit=limit,
        before=before,
        columns=_PAGE_SIZE_COLUMNS,
    )
    kept = _rows_within_content_budget(sizes)
    rows = (
        _page_rows(
            transaction,
            principal_id,
            room_id=room_id,
            thread_id=thread_id,
            limit=kept,
            before=before,
            columns=_PAGE_COLUMNS,
        )
        if kept
        else ()
    )
    decoded = _rows_within_decoded_budget(rows)
    trimmed = decoded < len(rows)
    rows = rows[:decoded]
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
        if rows and (trimmed or kept < len(sizes) or len(sizes) == limit)
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
    columns: str,
) -> tuple[Row, ...]:
    """Return one page's rows, newest first.

    A thread's root message carries no thread relation of its own — it becomes
    a root only when someone replies to it — so it is stored in the room
    conversation. Reading a thread therefore merges its replies with that one
    extra row, which is a primary-key lookup rather than a scan.
    """
    cursor_clause = "" if before is None else _CONVERSATION_CURSOR_CLAUSE
    cursor_params: tuple[object, ...] = () if before is None else (before.created_ts, before.logical_event_id)
    rows = list(
        transaction.fetchall(
            f"""
            SELECT {columns} FROM visible_messages
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
            SELECT {columns} FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?{cursor_clause}
            """,  # noqa: S608 - a fixed column list and a fixed clause, not input
            (principal_id, room_id, thread_id, *cursor_params),
        )
        if root is not None and all(row["logical_event_id"] != thread_id for row in rows):
            rows.append(root)
            rows.sort(key=lambda row: (int(row["created_ts"]), row["logical_event_id"]), reverse=True)
            del rows[limit:]
    return tuple(rows)


def _rows_within_content_budget(rows: tuple[Row, ...]) -> int:
    """Return how many of one page's newest rows fit its content budget, never fewer than one.

    A row owing a refetch holds no content and costs nothing, so resolving it
    can only shorten the next read of the same page, never lengthen it.
    """
    total = 0
    for index, row in enumerate(rows):
        total += int(row["content_bytes"] or 0)
        if total > _PAGE_CONTENT_BUDGET_BYTES:
            return max(index, 1)
    return len(rows)


def _rows_within_decoded_budget(rows: tuple[Row, ...]) -> int:
    """Return how many of one page's loaded rows fit what it may decode to, never fewer than one."""
    total = 0
    for index, row in enumerate(rows):
        content_json = row["content_json"] or ""
        total += (
            len(content_json)
            # Stored content is compact JSON: a structural object opens with an unescaped quote or closes at
            # once, and a structural separator is never followed by a space, so JSON text inside strings is free.
            + _DECODED_BYTES_PER_ARRAY * (content_json.count("[") - content_json.count('[\\"'))
            + _DECODED_BYTES_PER_OBJECT * (content_json.count('{"') + content_json.count("{}"))
            + _DECODED_BYTES_PER_SEPARATOR
            * (content_json.count(",") - content_json.count(", ") + content_json.count(":") - content_json.count(": "))
        )
        if total > _PAGE_DECODED_BUDGET_BYTES:
            return max(index, 1)
    return len(rows)


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

    Unless that revision was redacted. A message keeps its row when the edit
    currently on screen is deleted -- the body is withheld pending a refetch,
    but the message did not stop existing -- and quoting a redacted event
    renders as nothing. Its logical event is the answer in that window: a
    redaction of the logical event deletes the whole row, so a row that is
    still here has an original that is still in the room.

    A withheld body is not enough to tell those apart. A message whose text
    lives in a sidecar is stored exactly the same way -- no body, refresh owed
    -- and its revision is a perfectly good event that was never redacted. Only
    the tombstone distinguishes them, so only the tombstone is consulted.
    """
    row = transaction.fetchone(
        """
        SELECT CASE
                   WHEN EXISTS (
                       SELECT 1 FROM redaction_tombstones AS tombstone
                       WHERE tombstone.principal_id = visible.principal_id
                         AND tombstone.room_id = visible.room_id
                         AND tombstone.redacted_event_id = visible.revision_event_id
                   )
                   THEN visible.logical_event_id
                   ELSE visible.revision_event_id
               END AS reply_target
        FROM visible_messages AS visible
        WHERE visible.principal_id = ? AND visible.room_id = ? AND visible.thread_id = ?
        ORDER BY visible.created_ts DESC, visible.logical_event_id DESC
        LIMIT 1
        """,
        (principal_id, room_id, encode_thread_id(thread_id)),
    )
    return None if row is None else str(row["reply_target"])


def _current_hydration(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> Row | None:
    """Return this conversation's hydration row, if it still speaks for the room.

    A repairable recovery obligation withholds every marker in the room so the
    next read enters the repair path. A truncated obligation leaves its bounded
    context readable, but cannot certify completeness.
    """
    row = transaction.fetchone(
        """
        SELECT hydration.membership_epoch AS hydrated_epoch,
               hydration.complete AS complete,
               hydration.attempted_policy_rank AS attempted_policy_rank,
               COALESCE(membership.membership_epoch, 0) AS current_epoch,
               recovery.state AS recovery_state
        FROM conversation_hydration AS hydration
        LEFT JOIN room_membership AS membership
          ON membership.principal_id = hydration.principal_id
         AND membership.room_id = hydration.room_id
        LEFT JOIN room_history_recovery AS recovery
          ON recovery.principal_id = hydration.principal_id
         AND recovery.room_id = hydration.room_id
         AND recovery.state <> 'repaired'
        WHERE hydration.principal_id = ? AND hydration.room_id = ? AND hydration.thread_id = ?
        """,
        (principal_id, room_id, encode_thread_id(thread_id)),
    )
    if row is None or int(row["hydrated_epoch"]) != int(row["current_epoch"]):
        return None
    if row["recovery_state"] == "repairable":
        return None
    return row


def conversation_is_hydrated(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> bool:
    """Return whether this conversation was hydrated under the current membership."""
    return _current_hydration(transaction, principal_id, room_id=room_id, thread_id=thread_id) is not None


def conversation_is_complete(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> bool:
    """Return whether the walk that hydrated this conversation reached its end.

    Strictly stronger than being hydrated, and the two must not be confused. The
    hydration marker records that the one-time walk ran; this records that it
    ran out of conversation rather than out of allowance. A caller whose
    correctness is completeness rather than recency -- an export, not a prompt --
    asks this one, because a bounded walk leaves a warm marker over a partial
    conversation and nothing else distinguishes the two.

    A truncated recovery interval keeps this answer false even when its
    hydration marker would otherwise report a complete conversation.
    """
    row = _current_hydration(transaction, principal_id, room_id=room_id, thread_id=thread_id)
    return row is not None and bool(row["complete"]) and row["recovery_state"] is None


def conversation_hydration_coverage(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> HydrationCoverage | None:
    """Return what walks under this membership proved here, or nothing if none did.

    Everything a caller needs to decide whether walking again could change
    anything, and nothing else -- notably not history a skipped sync gap lost,
    which is what `conversation_is_complete` adds. That distinction is the
    difference between a reader deciding whether a conversation is whole and a
    hydrator deciding whether to walk it again, and conflating them made a
    lost-history room re-walk on every read to reach the same answer every
    time.

    Read as a whole record rather than as a predicate because the two facts in
    it answer to different owners. Whether a walk reached the start is a fact
    about the conversation; which policies have already been spent here only
    means something next to the caller's own policy, and only the caller knows
    that.

    Asked only by a caller that needs completeness. A prompt is served by the
    hydration marker alone, which is what keeps its warm reads free.
    """
    row = _current_hydration(transaction, principal_id, room_id=room_id, thread_id=thread_id)
    if row is None:
        return None
    return HydrationCoverage(
        reached_its_end=bool(row["complete"]) and row["recovery_state"] is None,
        attempted_policy_rank=int(row["attempted_policy_rank"]),
    )


def conversation_hydration_was_truncated(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
) -> bool:
    """Return whether a walk ran for this conversation and stopped at a ceiling.

    The negation of `conversation_is_complete` is not this, and the difference
    decides whether a prompt is allowed to call its page whole. A conversation
    with no hydration row is not complete, but nothing is missing from it
    either -- there was never anything to walk. Only a row that ran and gave up
    proves the page is a suffix.

    So an export, whose correctness is completeness, asks
    `conversation_is_complete` and refuses anything less. A prompt, whose
    correctness is recency, asks this and accepts everything except a proven
    truncation.

    A truncated recovery obligation is likewise a proven suffix, even when the
    hydration marker's own walk reached its end.
    """
    row = _current_hydration(transaction, principal_id, room_id=room_id, thread_id=thread_id)
    return row is not None and (not bool(row["complete"]) or row["recovery_state"] is not None)


def mark_conversation_hydrated(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    complete: bool,
    attempted_policy_rank: int,
    expected_membership_epoch: int,
) -> bool:
    """Publish a completed hydration, unless membership moved under it.

    Projection chunks remain untrusted until this final marker commits. Each
    chunk claims the same epoch before writing, and this transaction claims it
    once more before publishing coverage, so a fence between any two commits
    deletes the earlier rows and makes every later step refuse the stale epoch.

    Coverage only ever grows within one membership epoch. Two hydrators can
    finish in either order, and a narrower walk finishing last cannot overwrite
    the wider walk's proof that it reached the end or attempted a stronger
    policy. A later membership is a different view and clears both facts.
    """
    if not claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return False
    publish_conversation_hydration(
        transaction,
        principal_id,
        room_id=room_id,
        thread_id=thread_id,
        complete=complete,
        attempted_policy_rank=attempted_policy_rank,
        membership_epoch=expected_membership_epoch,
    )
    return True


def publish_conversation_hydration(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    thread_id: str | None,
    complete: bool,
    attempted_policy_rank: int,
    membership_epoch: int,
) -> None:
    """Publish hydration after the caller has claimed ``membership_epoch``."""
    transaction.execute(
        """
        INSERT INTO conversation_hydration (
            principal_id, room_id, thread_id, membership_epoch, complete, attempted_policy_rank
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, thread_id) DO UPDATE SET
            membership_epoch = excluded.membership_epoch,
            complete = CASE
                WHEN conversation_hydration.membership_epoch = excluded.membership_epoch
                     AND conversation_hydration.complete <> 0
                THEN conversation_hydration.complete
                ELSE excluded.complete
            END,
            attempted_policy_rank = CASE
                WHEN conversation_hydration.membership_epoch = excluded.membership_epoch
                     AND conversation_hydration.attempted_policy_rank > excluded.attempted_policy_rank
                THEN conversation_hydration.attempted_policy_rank
                ELSE excluded.attempted_policy_rank
            END
        """,
        (
            principal_id,
            room_id,
            encode_thread_id(thread_id),
            membership_epoch,
            int(complete),
            attempted_policy_rank,
        ),
    )


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
        revision_event_id=row["revision_event_id"],
        refresh_token=int(row["refresh_token"]),
        membership_epoch=int(row["membership_epoch"]),
    )
