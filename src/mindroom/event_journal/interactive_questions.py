"""Durable interactive questions and the journal sources selecting them."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from mindroom.interactive_models import (
    InteractivePrompt,
    InteractiveSelection,
    interactive_prompt_from_content,
)

from . import reads
from .identity import decode_thread_id
from .models import EventClass, EventKind, SemanticConsumer
from .schema import PENDING_STATE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .backend import Row, Transaction
    from .models import InboundEvent
    from .projection import ProjectedEvent


@dataclass(frozen=True, slots=True)
class _StoredSelection:
    """One source's immutable prompt snapshot."""

    selection: InteractiveSelection
    revision_event_id: str


def _consume_selection_revision(
    transaction: Transaction,
    principal_id: str,
    source_event_id: str,
    stored: _StoredSelection,
) -> None:
    """Consume one active revision and discard competing snapshots of it."""
    transaction.execute(
        """
        DELETE FROM interactive_questions
        WHERE principal_id = ? AND question_event_id = ? AND revision_event_id = ?
        """,
        (principal_id, stored.selection.question_event_id, stored.revision_event_id),
    )
    transaction.execute(
        """
        DELETE FROM interactive_selections
        WHERE principal_id = ? AND question_event_id = ? AND revision_event_id = ?
          AND source_event_id != ?
        """,
        (principal_id, stored.selection.question_event_id, stored.revision_event_id, source_event_id),
    )


def _prompt_json(prompt: InteractivePrompt) -> str:
    """Serialize one projected prompt payload deterministically."""
    return json.dumps(
        {
            "option_labels": prompt.option_labels,
            "options": prompt.options,
            "question_text": prompt.question_text,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _prompt_membership_epoch(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    prompt: InteractivePrompt,
) -> int | None:
    """Resolve the membership proof embedded in one prompt revision."""
    if prompt.source_event_id is not None:
        source = transaction.fetchone(
            """
            SELECT room_id, membership_epoch
            FROM journal_events
            WHERE principal_id = ? AND event_id = ?
            """,
            (principal_id, prompt.source_event_id),
        )
        if source is not None:
            return int(source["membership_epoch"]) if source["room_id"] == room_id else None
    return prompt.membership_epoch


def _visible_prompt_row(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    question_event_id: str,
) -> Row | None:
    """Return the projection's currently visible revision for one target."""
    return transaction.fetchone(
        """
        SELECT thread_id, sender, revision_event_id
        FROM visible_messages
        WHERE principal_id = ? AND room_id = ? AND logical_event_id = ?
        """,
        (principal_id, room_id, question_event_id),
    )


def _activate_projected_prompt(
    transaction: Transaction,
    principal_id: str,
    *,
    room_id: str,
    question_event_id: str,
    row: Row,
    prompt: InteractivePrompt,
) -> None:
    """Make one authorized visible revision the active prompt."""
    if principal_id != f"{prompt.creator_agent}@{row['sender']}":
        transaction.execute(
            "DELETE FROM interactive_questions WHERE principal_id = ? AND question_event_id = ?",
            (principal_id, question_event_id),
        )
        return
    membership_epoch = _prompt_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        prompt=prompt,
    )
    if membership_epoch is None or not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    ):
        transaction.execute(
            "DELETE FROM interactive_questions WHERE principal_id = ? AND question_event_id = ?",
            (principal_id, question_event_id),
        )
        return
    transaction.execute(
        """
        INSERT INTO interactive_questions (
            principal_id, question_event_id, revision_event_id, room_id, thread_id,
            question_json, membership_epoch, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, question_event_id) DO UPDATE SET
            revision_event_id = excluded.revision_event_id,
            room_id = excluded.room_id,
            thread_id = excluded.thread_id,
            question_json = excluded.question_json,
            membership_epoch = excluded.membership_epoch,
            created_at_ns = excluded.created_at_ns
        WHERE interactive_questions.revision_event_id != excluded.revision_event_id
        """,
        (
            principal_id,
            question_event_id,
            row["revision_event_id"],
            room_id,
            row["thread_id"],
            _prompt_json(prompt),
            membership_epoch,
            time.time_ns(),
        ),
    )


def reconcile_projected_prompt(
    transaction: Transaction,
    principal_id: str,
    projected: ProjectedEvent,
    installed_content: Mapping[str, object] | None,
) -> None:
    """Reconcile one active prompt from the projection after admission."""
    if projected.redacts_event_id is not None:
        transaction.execute(
            """
            DELETE FROM interactive_questions
            WHERE principal_id = ? AND (question_event_id = ? OR revision_event_id = ?)
            """,
            (principal_id, projected.redacts_event_id, projected.redacts_event_id),
        )
        return
    if installed_content is None:
        return
    relation = projected.content.get("m.relates_to")
    relation = cast("Mapping[str, object]", relation) if isinstance(relation, dict) else {}
    replacement = relation.get("event_id") if relation.get("rel_type") == "m.replace" else None
    question_event_id = replacement if isinstance(replacement, str) else projected.event_id
    row = _visible_prompt_row(
        transaction,
        principal_id,
        room_id=projected.room_id,
        question_event_id=question_event_id,
    )
    if row is None or not principal_id.endswith(f"@{row['sender']}"):
        return
    prompt = interactive_prompt_from_content(installed_content)
    if prompt is None:
        transaction.execute(
            "DELETE FROM interactive_questions WHERE principal_id = ? AND question_event_id = ?",
            (principal_id, question_event_id),
        )
        return
    _activate_projected_prompt(
        transaction,
        principal_id,
        room_id=projected.room_id,
        question_event_id=question_event_id,
        row=row,
        prompt=prompt,
    )


def _selection_from_row(row: Row, selection_key: str) -> InteractiveSelection | None:
    """Decode one validated selection from a stored question row."""
    payload = cast("dict[str, object]", json.loads(str(row["question_json"])))
    raw_options = cast("dict[object, object]", payload["options"])
    selected_value = raw_options.get(selection_key)
    if selected_value is None:
        return None
    raw_labels = cast("dict[object, object]", payload["option_labels"])
    return InteractiveSelection(
        question_event_id=str(row["question_event_id"]),
        question_text=str(payload["question_text"]),
        selection_key=selection_key,
        selected_label=str(raw_labels.get(selection_key, selected_value)),
        selected_value=str(selected_value),
        thread_id=decode_thread_id(str(row["thread_id"])),
    )


def _selection_json(selection: InteractiveSelection) -> str:
    """Serialize the immutable selection bound to one pending source."""
    return json.dumps(
        {
            "question_text": selection.question_text,
            "selected_label": selection.selected_label,
            "selected_value": selection.selected_value,
            "selection_key": selection.selection_key,
            "thread_id": selection.thread_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stored_selection(
    transaction: Transaction,
    principal_id: str,
    source_event_id: str,
) -> _StoredSelection | None:
    """Return the immutable prompt snapshot stored for one source."""
    row = transaction.fetchone(
        """
        SELECT question_event_id, revision_event_id, selection_json
        FROM interactive_selections
        WHERE principal_id = ? AND source_event_id = ?
        """,
        (principal_id, source_event_id),
    )
    if row is None:
        return None
    payload = cast("dict[str, object]", json.loads(str(row["selection_json"])))
    return _StoredSelection(
        selection=InteractiveSelection(
            question_event_id=str(row["question_event_id"]),
            question_text=str(payload["question_text"]),
            selection_key=str(payload["selection_key"]),
            selected_label=str(payload["selected_label"]),
            selected_value=str(payload["selected_value"]),
            thread_id=cast("str | None", payload["thread_id"]),
        ),
        revision_event_id=str(row["revision_event_id"]),
    )


def _snapshot_selection(
    transaction: Transaction,
    principal_id: str,
    source_event_id: str,
    revision_event_id: str,
    selection: InteractiveSelection,
) -> _StoredSelection:
    """Store and return one source-bound prompt revision snapshot."""
    transaction.execute(
        """
        INSERT INTO interactive_selections (
            principal_id, source_event_id, question_event_id,
            revision_event_id, selection_json
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, source_event_id) DO NOTHING
        """,
        (
            principal_id,
            source_event_id,
            selection.question_event_id,
            revision_event_id,
            _selection_json(selection),
        ),
    )
    return _StoredSelection(
        selection=selection,
        revision_event_id=revision_event_id,
    )


def _snapshot_reaction_candidate(  # noqa: PLR0911 - malformed or unrelated reactions have no candidate
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Snapshot the prompt revision visible when one reaction is admitted."""
    if event.kind is not EventKind.REACTION or event.event_class is not EventClass.ACTIONABLE:
        return
    content = event.source.get("content")
    if not isinstance(content, dict):
        return
    content = cast("dict[str, object]", content)
    relation = content.get("m.relates_to")
    if not isinstance(relation, dict):
        return
    relation = cast("dict[str, object]", relation)
    if relation.get("rel_type") != "m.annotation":
        return
    question_event_id = relation.get("event_id")
    selection_key = relation.get("key")
    if not isinstance(question_event_id, str) or not isinstance(selection_key, str):
        return
    source = transaction.fetchone(
        """
        SELECT membership_epoch
        FROM journal_events
        WHERE principal_id = ? AND event_id = ? AND state = ?
        """,
        (principal_id, event.event_id, PENDING_STATE),
    )
    if source is None:
        return
    membership_epoch = int(source["membership_epoch"])
    question_row = _question_row(transaction, principal_id, question_event_id)
    if (
        question_row is None
        or question_row["room_id"] != event.room_id
        or int(question_row["membership_epoch"]) != membership_epoch
    ):
        return
    selection = _selection_from_row(question_row, selection_key)
    if selection is None:
        return
    _snapshot_selection(
        transaction,
        principal_id,
        event.event_id,
        str(question_row["revision_event_id"]),
        selection,
    )


def _snapshot_text_candidate(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Snapshot the oldest prompt one numeric text answer can select."""
    if event.kind is not EventKind.MESSAGE or event.event_class is not EventClass.ACTIONABLE:
        return
    content = event.source.get("content")
    if not isinstance(content, dict):
        return
    body = cast("dict[str, object]", content).get("body")
    selection_key = body.strip() if isinstance(body, str) else ""
    if len(selection_key) != 1 or not selection_key.isdigit():
        return
    source = transaction.fetchone(
        """
        SELECT room_id, thread_id, membership_epoch
        FROM journal_events
        WHERE principal_id = ? AND event_id = ? AND state = ?
        """,
        (principal_id, event.event_id, PENDING_STATE),
    )
    if source is None:
        return
    question = transaction.fetchone(
        """
        SELECT question_event_id
        FROM interactive_questions
        WHERE principal_id = ? AND room_id = ? AND thread_id = ? AND membership_epoch = ?
        ORDER BY created_at_ns, question_event_id/*bytes*/
        LIMIT 1
        """,
        (principal_id, source["room_id"], source["thread_id"], source["membership_epoch"]),
    )
    if question is None:
        return
    question_row = _question_row(transaction, principal_id, str(question["question_event_id"]))
    if question_row is None or (selection := _selection_from_row(question_row, selection_key)) is None:
        return
    _snapshot_selection(
        transaction,
        principal_id,
        event.event_id,
        str(question_row["revision_event_id"]),
        selection,
    )


def snapshot_source_candidate(
    transaction: Transaction,
    principal_id: str,
    event: InboundEvent,
) -> None:
    """Freeze the prompt revision an admitted interactive answer saw."""
    if event.kind is EventKind.REACTION:
        _snapshot_reaction_candidate(transaction, principal_id, event)
    elif event.kind is EventKind.MESSAGE:
        _snapshot_text_candidate(transaction, principal_id, event)


def _source_row(transaction: Transaction, principal_id: str, source_event_id: str) -> Row | None:
    """Lock and return one still-pending source event."""
    return transaction.fetchone(
        """
        UPDATE journal_events
        SET state = state
        WHERE principal_id = ? AND event_id = ? AND state = ?
        RETURNING room_id, thread_id, kind, membership_epoch, semantic_consumer
        """,
        (principal_id, source_event_id, PENDING_STATE),
    )


def _question_row(transaction: Transaction, principal_id: str, question_event_id: str) -> Row | None:
    """Lock and return one question."""
    return transaction.fetchone(
        """
        UPDATE interactive_questions
        SET question_json = question_json
        WHERE principal_id = ? AND question_event_id = ?
        RETURNING question_event_id, room_id, thread_id,
                  revision_event_id, question_json, membership_epoch
        """,
        (principal_id, question_event_id),
    )


def claim_reaction(
    transaction: Transaction,
    principal_id: str,
    *,
    source_event_id: str,
    question_event_id: str,
    selection_key: str,
) -> InteractiveSelection | None:
    """Atomically transfer one valid question selection to its reaction source."""
    candidate = transaction.fetchone(
        """
        SELECT room_id, kind, membership_epoch, semantic_consumer
        FROM journal_events
        WHERE principal_id = ? AND event_id = ? AND state = ?
        """,
        (principal_id, source_event_id, PENDING_STATE),
    )
    if (
        candidate is None
        or EventKind(candidate["kind"]) is not EventKind.REACTION
        or candidate["semantic_consumer"] not in (None, SemanticConsumer.INTERACTIVE_REACTION.value)
    ):
        return None
    room_id = str(candidate["room_id"])
    membership_epoch = int(candidate["membership_epoch"])
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    ):
        return None

    source = _source_row(transaction, principal_id, source_event_id)
    if (
        source is None
        or source["room_id"] != room_id
        or int(source["membership_epoch"]) != membership_epoch
        or EventKind(source["kind"]) is not EventKind.REACTION
        or source["semantic_consumer"] not in (None, SemanticConsumer.INTERACTIVE_REACTION.value)
    ):
        return None
    stored_selection = _stored_selection(transaction, principal_id, source_event_id)
    if stored_selection is None:
        return None
    selection = stored_selection.selection
    if selection.question_event_id != question_event_id or selection.selection_key != selection_key:
        return None
    if source["semantic_consumer"] is None:
        transaction.execute(
            """
            UPDATE journal_events
            SET semantic_consumer = ?
            WHERE principal_id = ? AND event_id = ?
            """,
            (SemanticConsumer.INTERACTIVE_REACTION.value, principal_id, source_event_id),
        )
        _consume_selection_revision(transaction, principal_id, source_event_id, stored_selection)
    return selection


def claim_text(
    transaction: Transaction,
    principal_id: str,
    *,
    source_event_id: str,
    selection_key: str,
) -> InteractiveSelection | None:
    """Atomically claim the oldest matching question for one text source."""
    candidate_source = transaction.fetchone(
        """
        SELECT room_id, thread_id, kind, membership_epoch, semantic_consumer
        FROM journal_events
        WHERE principal_id = ? AND event_id = ? AND state = ?
        """,
        (principal_id, source_event_id, PENDING_STATE),
    )
    if (
        candidate_source is None
        or EventKind(candidate_source["kind"]) is not EventKind.MESSAGE
        or candidate_source["semantic_consumer"] is not None
    ):
        return None
    room_id = str(candidate_source["room_id"])
    stored_thread_id = str(candidate_source["thread_id"])
    thread_id = decode_thread_id(stored_thread_id)
    membership_epoch = int(candidate_source["membership_epoch"])
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=room_id,
        expected_membership_epoch=membership_epoch,
    ):
        return None
    source = _source_row(transaction, principal_id, source_event_id)
    if (
        source is None
        or source["room_id"] != room_id
        or decode_thread_id(str(source["thread_id"])) != thread_id
        or int(source["membership_epoch"]) != membership_epoch
        or EventKind(source["kind"]) is not EventKind.MESSAGE
        or source["semantic_consumer"] is not None
    ):
        return None
    stored_selection = _stored_selection(transaction, principal_id, source_event_id)
    if stored_selection is None or stored_selection.selection.selection_key != selection_key:
        return None
    _consume_selection_revision(transaction, principal_id, source_event_id, stored_selection)
    return stored_selection.selection
