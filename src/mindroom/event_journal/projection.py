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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .identity import encode_thread_id

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Transaction

RELATES_TO = "m.relates_to"
REL_TYPE = "rel_type"
REPLACE_REL_TYPE = "m.replace"
THREAD_REL_TYPE = "m.thread"
NEW_CONTENT = "m.new_content"


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
    relation = content.get(RELATES_TO)
    return relation if isinstance(relation, dict) else {}


def replacement_target(content: Mapping[str, object]) -> str | None:
    """Return the event this content replaces, if it is an edit."""
    relation = _relation(content)
    if relation.get(REL_TYPE) != REPLACE_REL_TYPE:
        return None
    target = relation.get("event_id")
    return target if isinstance(target, str) and target else None


def thread_root(content: Mapping[str, object]) -> str | None:
    """Return the thread this content belongs to, if any."""
    relation = _relation(content)
    if relation.get(REL_TYPE) != THREAD_REL_TYPE:
        return None
    root = relation.get("event_id")
    return root if isinstance(root, str) and root else None


def visible_content(content: Mapping[str, object]) -> Mapping[str, object]:
    """Return the body an edit installs, which lives under ``m.new_content``."""
    new_content = content.get(NEW_CONTENT)
    return new_content if isinstance(new_content, dict) else content


def _is_newer(candidate: tuple[int, str], current: tuple[int, str]) -> bool:
    """Order revisions by ``(origin_server_ts, event_id)``.

    Timestamps alone are not a total order: two edits can share a millisecond,
    and clients disagree about clocks. The event ID breaks the tie so every
    replica of this projection reaches the same visible revision.
    """
    return candidate > current


def _dumps(content: Mapping[str, object]) -> str:
    return json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _loads(content_json: str) -> Mapping[str, object]:
    decoded = json.loads(content_json)
    if not isinstance(decoded, dict):
        msg = "Projected content must be a JSON object"
        raise ValueError(msg)
    return decoded


def is_tombstoned(
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


def record_tombstone(
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
    if is_tombstoned(transaction, principal_id, event.room_id, event.event_id):
        return
    replaces = replacement_target(event.content)
    if replaces is None:
        _project_original(transaction, principal_id, event, membership_epoch=membership_epoch)
        return
    _project_edit(
        transaction,
        principal_id,
        event,
        target_event_id=replaces,
        membership_epoch=membership_epoch,
    )


def _project_original(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    membership_epoch: int,
) -> None:
    """Install a new logical message and apply an edit that beat it here."""
    transaction.execute(
        """
        INSERT INTO visible_messages (
            principal_id, room_id, logical_event_id, thread_id, sender,
            created_ts, revision_event_id, revision_ts, content_json,
            refresh_token, membership_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
            _dumps(event.content),
            membership_epoch,
        ),
    )
    _apply_unresolved_edit(transaction, principal_id, event)


def _apply_unresolved_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
) -> None:
    """Apply the original sender's held edit, then drop every held edit.

    Unresolved edits are keyed by sender as well as target. Without the sender
    in the key, anyone in the room could send an edit for a message that has not
    arrived yet and evict the author's real edit before it could apply.
    """
    held = transaction.fetchone(
        """
        SELECT edit_event_id, edit_ts, content_json FROM unresolved_edits
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
    if is_tombstoned(transaction, principal_id, event.room_id, held["edit_event_id"]):
        return
    _install_revision(
        transaction,
        principal_id,
        room_id=event.room_id,
        logical_event_id=event.event_id,
        revision_event_id=held["edit_event_id"],
        revision_ts=int(held["edit_ts"]),
        content=visible_content(_loads(held["content_json"])),
    )


def _project_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    target_event_id: str,
    membership_epoch: int,
) -> None:
    """Replace the target's visible body, or hold the edit until it arrives."""
    current = transaction.fetchone(
        """
        SELECT sender, revision_event_id, revision_ts FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (principal_id, event.room_id, target_event_id),
    )
    if current is None:
        if is_tombstoned(transaction, principal_id, event.room_id, target_event_id):
            return
        _hold_unresolved_edit(
            transaction,
            principal_id,
            event,
            target_event_id=target_event_id,
            membership_epoch=membership_epoch,
        )
        return
    if current["sender"] != event.sender:
        return
    if not _is_newer(
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
    )


def _hold_unresolved_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    target_event_id: str,
    membership_epoch: int,
) -> None:
    """Keep at most one latest edit per target and sender."""
    del membership_epoch
    held = transaction.fetchone(
        """
        SELECT edit_event_id, edit_ts FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ? AND sender = ?
        """,
        (principal_id, event.room_id, target_event_id, event.sender),
    )
    if held is not None and not _is_newer(
        (event.origin_server_ts, event.event_id),
        (int(held["edit_ts"]), held["edit_event_id"]),
    ):
        return
    transaction.execute(
        """
        INSERT INTO unresolved_edits (
            principal_id, room_id, target_event_id, sender,
            edit_event_id, edit_ts, thread_id, content_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, target_event_id, sender) DO UPDATE SET
            edit_event_id = excluded.edit_event_id,
            edit_ts = excluded.edit_ts,
            thread_id = excluded.thread_id,
            content_json = excluded.content_json
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
) -> None:
    transaction.execute(
        """
        UPDATE visible_messages
        SET revision_event_id = ?, revision_ts = ?, content_json = ?, refresh_token = NULL
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (
            revision_event_id,
            revision_ts,
            _dumps(content),
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
    record_tombstone(transaction, principal_id, event.room_id, target, receipt_order)
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
    """
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


def decode_content(content_json: Any) -> Mapping[str, object]:
    """Decode one stored visible body."""
    return _loads(content_json)
