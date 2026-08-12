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
) -> None:
    """Remember a redaction before projecting it.

    Recorded first so that an original or edit arriving later — a real ordering
    on a server that backfills — cannot resurrect content the sender deleted.

    The row is the whole fact. Every reader asks only whether one event is
    tombstoned, so when the redaction was received is not part of the answer.
    """
    transaction.execute(
        """
        INSERT INTO redaction_tombstones (principal_id, room_id, redacted_event_id)
        VALUES (?, ?, ?)
        ON CONFLICT (principal_id, room_id, redacted_event_id) DO NOTHING
        """,
        (principal_id, room_id, redacted_event_id),
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
    )


def _project_original(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    receipt_order: int,
    membership_epoch: int,
) -> None:
    """Install a new logical message and apply an edit that beat it here.

    Every event reaching here came from sync, so a repeat is the same event
    twice and changes nothing. An edit that arrived before the message it
    revises was held, and is applied now that its target exists.
    """
    content_json, refresh_token = _stored_body(event.content, receipt_order)
    transaction.execute(
        """
        INSERT INTO visible_messages (
            principal_id, room_id, logical_event_id, thread_id, sender,
            created_ts, revision_event_id, revision_ts, content_json,
            refresh_token, membership_epoch
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    )


def _project_edit(
    transaction: Transaction,
    principal_id: str,
    event: ProjectedEvent,
    *,
    target_event_id: str,
    receipt_order: int,
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
        if _is_tombstoned(transaction, principal_id, event.room_id, target_event_id):
            return
        _hold_unresolved_edit(transaction, principal_id, event, target_event_id=target_event_id)
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
        receipt_order=receipt_order,
    )


def _held_edit_yields_to(held: Row, event: ProjectedEvent) -> bool:
    """Return whether an incoming edit should replace the one already held.

    The same rule the installed-revision path uses, because a held edit is the
    installed revision of a message that has not arrived yet.
    """
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
) -> None:
    """Keep at most one latest edit per target and sender.

    A held edit is not scoped to a membership. It survives until its target
    arrives or that target is redacted, whichever happens first, and the fence
    deletes the whole table for the room it invalidates, so there is nothing an
    epoch on the row could decide.

    Both endings have to be spelled out, because the row holds a message body
    and only one of them is the ordinary case. A target that is redacted before
    it ever arrives never lands afterwards -- ``project`` turns it away at the
    tombstone -- so the arrival this row is waiting for is not coming, and
    ``_project_redaction`` is the only thing left to collect it.

    A target that neither arrives nor is redacted has no third ending, and the
    membership fence is the whole bound on it. That is deliberate: the row is
    the only record of an edit whose message may still be one sync away.
    """
    held = transaction.fetchone(
        """
        SELECT edit_event_id, edit_ts FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ? AND sender = ?
        """,
        (principal_id, event.room_id, target_event_id, event.sender),
    )
    if held is not None and not _held_edit_yields_to(held, event):
        return
    transaction.execute(
        """
        INSERT INTO unresolved_edits (
            principal_id, room_id, target_event_id, sender,
            edit_event_id, edit_ts, content_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, room_id, target_event_id, sender) DO UPDATE SET
            edit_event_id = excluded.edit_event_id,
            edit_ts = excluded.edit_ts,
            content_json = excluded.content_json
        """,
        (
            principal_id,
            event.room_id,
            target_event_id,
            event.sender,
            event.event_id,
            event.origin_server_ts,
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
    receipt_order: int,
) -> None:
    content_json, refresh_token = _stored_body(content, receipt_order)
    transaction.execute(
        """
        UPDATE visible_messages
        SET revision_event_id = ?, revision_ts = ?, content_json = ?, refresh_token = ?
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (
            revision_event_id,
            revision_ts,
            content_json,
            refresh_token,
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
    _record_tombstone(transaction, principal_id, event.room_id, target)
    transaction.execute(
        """
        DELETE FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND edit_event_id = ?
        """,
        (principal_id, event.room_id, target),
    )
    # Held edits for this target, and not only when the target is visible. An
    # edit is held only while its target has no visible row, and the insert
    # that makes one visible drops the held edits for it in the same
    # transaction -- so the two states never coexist, and this delete guarded
    # on the visible row matched nothing every time it ran.
    #
    # Unguarded it collects the case that has no other ending. A target that is
    # redacted before it arrives never arrives afterwards either, because
    # `project` turns it away at the tombstone, so the arrival the row waits
    # for is not coming. It would otherwise outlive the redaction for the whole
    # membership epoch holding the only copy of the replacement text left on
    # this host, settlement having already blanked the journal's `source_json`.
    transaction.execute(
        """
        DELETE FROM unresolved_edits
        WHERE principal_id = ? AND room_id = ? AND target_event_id = ?
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

    The revision is checked against the tombstone table for a reason the token
    cannot cover. Redacting a revision that is not the one on screen matches no
    row, so it correctly moves no token -- but it does record a tombstone. A
    refetch already in flight, having chosen that very revision from the server,
    then still matches the token it was issued and would install a body the
    sender deleted. Nothing later disturbs it: hydration does not re-run under
    the same membership, so the deleted text would be served to every prompt,
    summary and export of that room from then on. Every other install path
    already asks the tombstone table; this was the one that did not.
    """
    if holds_unresolved_sidecar(content):
        return False
    if _is_tombstoned(transaction, principal_id, room_id, revision_event_id):
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
