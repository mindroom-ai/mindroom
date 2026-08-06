"""The visible-message projection: one row per logical message.

Every rule here runs inside the admission transaction, so the projection can
never disagree with the journal about what was admitted.

The projection deliberately keeps no edit history. An edit overwrites the
visible row; the previous body is gone. That is what makes streaming edit churn
free, and it is why redacting the currently visible revision has to ask the
homeserver for the new truth instead of popping a local stack.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.matrix.sidecar_content import holds_unresolved_sidecar

from .identity import encode_thread_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction

_RELATES_TO = "m.relates_to"
_REL_TYPE = "rel_type"
_REPLACE_REL_TYPE = "m.replace"
_THREAD_REL_TYPE = "m.thread"
_NEW_CONTENT = "m.new_content"
# A seeded row is this bot's own account of what it just sent, so it has no
# journal receipt to name. It only ever has to be a token that exists; the
# echo of the same message overwrites the row with a real one.
_SEED_REFRESH_TOKEN = 0


@dataclass(frozen=True, slots=True)
class ProjectedEvent:
    """One event's projection-relevant shape, extracted from its Matrix source."""

    event_id: str
    room_id: str
    thread_id: str | None
    sender: str
    origin_server_ts: int
    content: Mapping[str, object]
    replaces_event_id: str | None
    redacts_event_id: str | None


def _relation(content: Mapping[str, object]) -> Mapping[str, object]:
    relation = content.get(_RELATES_TO)
    return cast("Mapping[str, object]", relation) if isinstance(relation, dict) else {}


def replacement_target(content: Mapping[str, object]) -> str | None:
    """Return the event this content replaces, if it is an edit."""
    relation = _relation(content)
    if relation.get(_REL_TYPE) != _REPLACE_REL_TYPE:
        return None
    target = relation.get("event_id")
    return target if isinstance(target, str) and target else None


def thread_root(content: Mapping[str, object]) -> str | None:
    """Return the thread this content belongs to, if any."""
    relation = _relation(content)
    if relation.get(_REL_TYPE) != _THREAD_REL_TYPE:
        return None
    root = relation.get("event_id")
    return root if isinstance(root, str) and root else None


def visible_content(content: Mapping[str, object]) -> Mapping[str, object]:
    """Return the body an edit installs, which lives under ``m.new_content``."""
    new_content = content.get(_NEW_CONTENT)
    return cast("Mapping[str, object]", new_content) if isinstance(new_content, dict) else content


def is_newer_revision(candidate: tuple[int, str], current: tuple[int, str]) -> bool:
    """Order revisions by ``(origin_server_ts, event_id)``.

    Timestamps alone are not a total order: two edits can share a millisecond,
    and clients disagree about clocks. The event ID breaks the tie so every
    replica of this projection reaches the same visible revision.

    Hydration reduces a fetched relation tree with this same rule, so a
    conversation looks identical whether it was built from live events or
    reconstructed from the server.
    """
    return candidate > current


_is_newer = is_newer_revision


def _dumps(content: Mapping[str, object]) -> str:
    return json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _stored_body(content: Mapping[str, object], receipt_order: int) -> tuple[str | None, int | None]:
    """Return how one revision's body is stored, and what it still owes.

    A message too large for a single Matrix event carries a truncated preview
    in its content and its real text in an attached file. Storing that preview
    would hand every reader a body that looks complete and is not, and no
    reader can tell the difference by inspecting it.

    So the row records the same shape a redaction leaves behind: no body, and a
    refresh token. Reads omit it and report it as owed rather than serving it,
    and the existing resolver repairs it. Resolved content carries no sidecar
    metadata of its own, so storing the resolution is what clears the debt --
    nothing has to remember to.
    """
    if holds_unresolved_sidecar(content):
        return None, receipt_order
    return _dumps(content), None


def _loads(content_json: str) -> Mapping[str, object]:
    decoded = json.loads(content_json)
    if not isinstance(decoded, dict):
        msg = "Projected content must be a JSON object"
        raise TypeError(msg)
    return cast("Mapping[str, object]", decoded)


def _is_tombstoned(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    event_id: str,
) -> bool:
    """Return whether one event was already redacted."""
    row = transaction.fetchone(
        """
        SELECT 1 AS present FROM redaction_tombstones
        WHERE principal_id = ? AND room_id = ? AND redacted_event_id = ?
        """,
        (principal_id, room_id, event_id),
    )
    return row is not None


def _record_tombstone(
    transaction: Transaction,
    principal_id: str,
    room_id: str,
    redacted_event_id: str,
    receipt_order: int,
) -> None:
    """Remember a redaction before projecting it.

    Recorded first so that an original or edit arriving later — a real ordering
    on a server that backfills — cannot resurrect content the sender deleted.
    """
    transaction.execute(
        """
        INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id, receipt_order)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, redacted_event_id) DO NOTHING
        """,
        (principal_id, room_id, redacted_event_id, receipt_order),
    )


def project(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    receipt_order: int,
    membership_epoch: int,
) -> None:
    """Fold one admitted event into the visible-message projection."""
    if event.redacts_event_id is not None:
        _project_redaction(
            transaction,
            principal_id,
            event,
            receipt_order=receipt_order,
        )
        return
    if _is_tombstoned(transaction, principal_id, event.room_id, event.event_id):
        return
    replaces = replacement_target(event.content)
    if replaces is None:
        _project_original(
            transaction,
            principal_id,
            event,
            receipt_order=receipt_order,
            membership_epoch=membership_epoch,
        )
        return
    _project_edit(
        transaction,
        principal_id,
        event,
        target_event_id=replaces,
        receipt_order=receipt_order,
        membership_epoch=membership_epoch,
        provisional=False,
    )


def seed_outbound(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    membership_epoch: int,
) -> None:
    """Record what this bot just sent, before its echo comes back.

    An edit takes the ordinary edit path, because there is nothing provisional
    about which logical message it revises — only about when. The echo of that
    same revision reinstalls it with the server's timestamp.

    Waiting for the echo is free for a turn a room event triggered, because
    the timeline orders this message before the user's next one. It is not
    free for a turn that reads the conversation after speaking in it, or for
    one no room event triggered at all — a scheduled task, a todo poke. Those
    would read a room they have already spoken in as one they have not, and
    a strict read cannot express "wait for my own echo".

    What is written is deliberately provisional. The send response carries an
    event ID and nothing else; the timestamp here is this machine's clock, and
    ordering is the server's to decide. The echo replaces it.
    """
    replaces = replacement_target(event.content)
    if replaces is not None:
        # Never let a local clock claim a position in the future. The seed only
        # has to outrank the revision it replaces; giving it the wall clock lets
        # it outrank a genuine later edit that has not been echoed yet, and that
        # edit is then rejected for good -- canonicalizing the seed afterwards
        # does not bring it back.
        _project_edit(
            transaction,
            principal_id,
            replace(event, origin_server_ts=_seed_ordering_key(transaction, principal_id, event, replaces)),
            target_event_id=replaces,
            receipt_order=_SEED_REFRESH_TOKEN,
            membership_epoch=membership_epoch,
            provisional=True,
        )
        return
    content_json, refresh_token = _stored_body(event.content, _SEED_REFRESH_TOKEN)
    transaction.execute(
        """
        INSERT INTO visible_messages (
            principal_id, room_id, logical_event_id, thread_id, sender,
            created_ts, revision_event_id, revision_ts, content_json,
            refresh_token, membership_epoch, provisional, revision_provisional
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
        ON CONFLICT (principal_id, room_id, logical_event_id) DO NOTHING
        """,
        (
            principal_id,
            event.room_id,
            event.event_id,
            encode_thread_id(event.thread_id),
            event.sender,
            event.origin_server_ts,
            event.event_id,
            event.origin_server_ts,
            content_json,
            refresh_token,
            membership_epoch,
        ),
    )


def _seed_ordering_key(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    target_event_id: str,
) -> int:
    """Return an ordering key one step past the revision this seed replaces.

    A seeded edit is the newest thing this bot knows about, and nothing more.
    Anything the server stamps later must still be able to beat it.
    """
    current = transaction.fetchone(
        """
        SELECT revision_ts FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (principal_id, event.room_id, target_event_id),
    )
    if current is not None:
        return int(current["revision_ts"]) + 1
    # The original has not arrived, so this seed is held. It still has to
    # outrank the seed before it: collapsing every held seed onto zero would
    # order them by event ID, and the bot's own send order -- the one thing
    # seeding exists to preserve -- would be decided by a string comparison.
    # These keys stay near zero and so cannot reach a real server timestamp.
    held = transaction.fetchone(
        """
        SELECT edit_ts FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ? AND sender = ?
        """,
        (principal_id, event.room_id, target_event_id, event.sender),
    )
    return 0 if held is None else int(held["edit_ts"]) + 1


def _project_original(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    receipt_order: int,
    membership_epoch: int,
) -> None:
    """Install a new logical message and apply an edit that beat it here.

    A row this bot seeded from its own send is the one case where an existing
    row must yield. Its ordering metadata was a local guess, and this event is
    the server's own account of the same message, so the echo is what makes it
    authoritative. Every other repeat is a genuine duplicate and changes
    nothing, which is why the update is guarded rather than unconditional.
    """
    content_json, refresh_token = _stored_body(event.content, receipt_order)
    transaction.execute(
        """
        INSERT INTO visible_messages (
            principal_id, room_id, logical_event_id, thread_id, sender,
            created_ts, revision_event_id, revision_ts, content_json,
            refresh_token, membership_epoch, provisional, revision_provisional
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
        ON CONFLICT (principal_id, room_id, logical_event_id) DO UPDATE SET
            thread_id = excluded.thread_id,
            sender = excluded.sender,
            created_ts = excluded.created_ts,
            revision_event_id = excluded.revision_event_id,
            revision_ts = excluded.revision_ts,
            content_json = excluded.content_json,
            refresh_token = excluded.refresh_token,
            membership_epoch = excluded.membership_epoch,
            provisional = 0,
            revision_provisional = 0
        WHERE visible_messages.provisional = 1
          AND visible_messages.revision_event_id = visible_messages.logical_event_id
        """,
        (
            principal_id,
            event.room_id,
            event.event_id,
            encode_thread_id(event.thread_id),
            event.sender,
            event.origin_server_ts,
            event.event_id,
            event.origin_server_ts,
            content_json,
            refresh_token,
            membership_epoch,
        ),
    )
    # An edit can arrive before the echo of the message it edits. The revision
    # then belongs to the edit and must survive, but the creation time is still
    # a guess and the row is still provisional, so both are corrected here.
    transaction.execute(
        """
        UPDATE visible_messages SET created_ts = ?, provisional = 0
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ? AND provisional = 1
        """,
        (event.origin_server_ts, principal_id, event.room_id, event.event_id),
    )
    _apply_unresolved_edit(transaction, principal_id, event, receipt_order=receipt_order)


def _apply_unresolved_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    receipt_order: int,
) -> None:
    """Apply the original sender's held edit, then drop every held edit.

    Unresolved edits are keyed by sender as well as target. Without the sender
    in the key, anyone in the room could send an edit for a message that has not
    arrived yet and evict the author's real edit before it could apply.
    """
    held = transaction.fetchone(
        """
        SELECT edit_event_id, edit_ts, content_json, provisional FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ? AND sender = ?
        """,
        (principal_id, event.room_id, event.event_id, event.sender),
    )
    transaction.execute(
        """
        DELETE FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ?
        """,
        (principal_id, event.room_id, event.event_id),
    )
    if held is None:
        return
    if _is_tombstoned(transaction, principal_id, event.room_id, held["edit_event_id"]):
        return
    _install_revision(
        transaction,
        principal_id,
        room_id=event.room_id,
        logical_event_id=event.event_id,
        revision_event_id=held["edit_event_id"],
        revision_ts=int(held["edit_ts"]),
        content=visible_content(_loads(held["content_json"])),
        receipt_order=receipt_order,
        # A held edit this bot seeded is still a guess about ordering. Losing
        # that here would install a locally timed revision as authoritative,
        # and its own echo would then look like a stale duplicate.
        provisional=bool(held["provisional"]),
    )


def _project_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    target_event_id: str,
    receipt_order: int,
    membership_epoch: int,
    provisional: bool,
) -> None:
    """Replace the target's visible body, or hold the edit until it arrives."""
    current = transaction.fetchone(
        """
        SELECT sender, revision_event_id, revision_ts, revision_provisional FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (principal_id, event.room_id, target_event_id),
    )
    if current is None:
        if _is_tombstoned(transaction, principal_id, event.room_id, target_event_id):
            return
        _hold_unresolved_edit(
            transaction,
            principal_id,
            event,
            target_event_id=target_event_id,
            membership_epoch=membership_epoch,
            provisional=provisional,
        )
        return
    if current["sender"] != event.sender:
        return
    # The server's account of a revision this bot seeded replaces the local
    # guess outright: comparing would reject it whenever this machine's clock
    # runs ahead, and every later edit would then lose to a revision stamped in
    # the future.
    #
    # The direction matters and identity alone cannot express it. A seed that
    # arrives *after* its own echo -- ordinary, because the send awaits the
    # network before it seeds -- has the same event ID as the installed
    # revision while carrying strictly worse information. Letting that through
    # puts the local clock back on an authoritative revision and freezes the
    # answer there.
    already_authoritative = current["revision_event_id"] == event.event_id and not current["revision_provisional"]
    if provisional and already_authoritative:
        # The echo won the race. This seed carries the same revision with a
        # worse timestamp, and its clock may well be ahead, so it would win a
        # comparison it has no business winning.
        return
    canonicalizes = not provisional and current["revision_event_id"] == event.event_id and not already_authoritative
    if not canonicalizes and not _is_newer(
        (event.origin_server_ts, event.event_id),
        (int(current["revision_ts"]), current["revision_event_id"]),
    ):
        return
    _install_revision(
        transaction,
        principal_id,
        room_id=event.room_id,
        logical_event_id=target_event_id,
        revision_event_id=event.event_id,
        revision_ts=event.origin_server_ts,
        content=visible_content(event.content),
        receipt_order=receipt_order,
        provisional=provisional,
    )


def _held_edit_yields_to(held: Row, event: ProjectedEvent, *, provisional: bool) -> bool:
    """Return whether an incoming edit should replace the one already held.

    The same rules the installed-revision path uses, because a held edit is
    the installed revision of a message that has not arrived yet. Comparing
    timestamps alone lets a locally seeded edit outrank the server's own
    account of that very edit, and then the original's arrival makes the guess
    permanent.
    """
    same_edit = held["edit_event_id"] == event.event_id
    if same_edit:
        # One of these is the echo of the other. Authority decides, not time.
        return not provisional and bool(held["provisional"])
    return _is_newer(
        (event.origin_server_ts, event.event_id),
        (int(held["edit_ts"]), held["edit_event_id"]),
    )


def _hold_unresolved_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    target_event_id: str,
    membership_epoch: int,
    provisional: bool,
) -> None:
    """Keep at most one latest edit per target and sender."""
    del membership_epoch
    held = transaction.fetchone(
        """
        SELECT edit_event_id, edit_ts, provisional FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ? AND sender = ?
        """,
        (principal_id, event.room_id, target_event_id, event.sender),
    )
    if held is not None and not _held_edit_yields_to(held, event, provisional=provisional):
        return
    transaction.execute(
        """
        INSERT INTO unresolved_edits (
            principal_id, room_id, target_event_id, sender,
            edit_event_id, edit_ts, thread_id, content_json, provisional
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, target_event_id, sender) DO UPDATE SET
            edit_event_id = excluded.edit_event_id,
            edit_ts = excluded.edit_ts,
            thread_id = excluded.thread_id,
            content_json = excluded.content_json,
            provisional = excluded.provisional
        """,
        (
            principal_id,
            event.room_id,
            target_event_id,
            event.sender,
            event.event_id,
            event.origin_server_ts,
            encode_thread_id(event.thread_id),
            _dumps(event.content),
            1 if provisional else 0,
        ),
    )


def _install_revision(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    logical_event_id: str,
    revision_event_id: str,
    revision_ts: int,
    content: Mapping[str, object],
    receipt_order: int,
    provisional: bool = False,
) -> None:
    content_json, refresh_token = _stored_body(content, receipt_order)
    transaction.execute(
        """
        UPDATE visible_messages
        SET revision_event_id = ?, revision_ts = ?, content_json = ?, refresh_token = ?,
            revision_provisional = ?
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (
            revision_event_id,
            revision_ts,
            content_json,
            refresh_token,
            1 if provisional else 0,
            principal_id,
            room_id,
            logical_event_id,
        ),
    )


def _project_redaction(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    receipt_order: int,
) -> None:
    """Apply a redaction to whatever the target turns out to be."""
    target = event.redacts_event_id
    if target is None:
        return
    _record_tombstone(transaction, principal_id, event.room_id, target, receipt_order)
    transaction.execute(
        """
        DELETE FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND edit_event_id = ?
        """,
        (principal_id, event.room_id, target),
    )
    logical = transaction.fetchone(
        """
        SELECT logical_event_id FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (principal_id, event.room_id, target),
    )
    if logical is not None:
        transaction.execute(
            """
            DELETE FROM visible_messages
            WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
            """,
            (principal_id, event.room_id, target),
        )
        transaction.execute(
            """
            DELETE FROM unresolved_edits
            WHERE principal_id = ? AND room_id = ? AND target_event_id = ?
            """,
            (principal_id, event.room_id, target),
        )
        return
    # Redacting the revision that is currently on screen. The body must stop
    # being readable in this same transaction; the server-authoritative
    # replacement arrives later through a point refetch. Redacting an already
    # superseded edit matches nothing here and correctly changes nothing.
    transaction.execute(
        """
        UPDATE visible_messages
        SET content_json = NULL, refresh_token = ?
        WHERE principal_id = ? AND room_id = ? AND revision_event_id = ?
        """,
        (receipt_order, principal_id, event.room_id, target),
    )


def install_refetched_revision(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    logical_event_id: str,
    revision_event_id: str,
    revision_ts: int,
    content: Mapping[str, object],
    expected_refresh_token: int,
    expected_membership_epoch: int,
) -> bool:
    """Install a refetched revision only if nothing changed underneath it.

    A newer edit or redaction landing while the refetch was in flight moves the
    refresh token, so this conditional update is what stops a slow refetch from
    overwriting fresher truth. Returning ``False`` leaves the token durable and
    the message unreadable, which is the safe direction.

    Content that still holds a sidecar reference is refused for the same
    reason. A refetch returns the event as the server stored it, preview and
    all, so installing it unread would resolve the debt by satisfying it with
    the very text it was raised about.
    """
    if holds_unresolved_sidecar(content):
        return False
    row = transaction.fetchone(
        """
        UPDATE visible_messages
        SET revision_event_id = ?, revision_ts = ?, content_json = ?, refresh_token = NULL
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
          AND refresh_token = ? AND membership_epoch = ?
        RETURNING logical_event_id
        """,
        (
            revision_event_id,
            revision_ts,
            _dumps(content),
            principal_id,
            room_id,
            logical_event_id,
            expected_refresh_token,
            expected_membership_epoch,
        ),
    )
    return row is not None


def drop_refetched_message(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    logical_event_id: str,
    expected_refresh_token: int,
    expected_membership_epoch: int,
) -> bool:
    """Remove a logical message the server no longer has any revision of."""
    row = transaction.fetchone(
        """
        DELETE FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
          AND refresh_token = ? AND membership_epoch = ?
        RETURNING logical_event_id
        """,
        (
            principal_id,
            room_id,
            logical_event_id,
            expected_refresh_token,
            expected_membership_epoch,
        ),
    )
    return row is not None


def decode_content(content_json: str) -> Mapping[str, object]:
    """Decode one stored visible body."""
    return _loads(content_json)
