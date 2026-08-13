"""Durable interactive questions and the journal sources selecting them."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, cast

from mindroom.interactive_models import InteractiveQuestion, InteractiveSelection

from . import reads
from .identity import decode_thread_id, encode_thread_id
from .models import EventKind, SemanticConsumer
from .schema import PENDING_STATE

if TYPE_CHECKING:
    from .backend import Row, Transaction


def _question_json(question: InteractiveQuestion) -> str:
    """Serialize the immutable question payload deterministically."""
    return json.dumps(
        {
            "option_labels": dict(question.option_labels),
            "options": dict(question.options),
            "question_text": question.question_text,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
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
    """Serialize the immutable selection owned by one pending source."""
    return json.dumps(
        {
            "question_event_id": selection.question_event_id,
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
) -> InteractiveSelection | None:
    """Return the immutable selection already owned by one source."""
    row = transaction.fetchone(
        """
        SELECT selection_json
        FROM interactive_selections
        WHERE principal_id = ? AND source_event_id = ?
        """,
        (principal_id, source_event_id),
    )
    if row is None:
        return None
    payload = cast("dict[str, object]", json.loads(str(row["selection_json"])))
    return InteractiveSelection(
        question_event_id=str(payload["question_event_id"]),
        question_text=str(payload["question_text"]),
        selection_key=str(payload["selection_key"]),
        selected_label=str(payload["selected_label"]),
        selected_value=str(payload["selected_value"]),
        thread_id=cast("str | None", payload["thread_id"]),
    )


def _store_selection(
    transaction: Transaction,
    principal_id: str,
    source_event_id: str,
    selection: InteractiveSelection,
) -> None:
    """Transfer one active question into its source-owned selection."""
    transaction.execute(
        """
        INSERT INTO interactive_selections (principal_id, source_event_id, selection_json)
        VALUES (?, ?, ?)
        """,
        (principal_id, source_event_id, _selection_json(selection)),
    )
    transaction.execute(
        """
        DELETE FROM interactive_questions
        WHERE principal_id = ? AND question_event_id = ?
        """,
        (principal_id, selection.question_event_id),
    )


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
        RETURNING question_event_id, room_id, thread_id, creator_agent,
                  question_json, membership_epoch
        """,
        (principal_id, question_event_id),
    )


def claim_reaction(  # noqa: PLR0911 - each failed ownership predicate is terminal
    transaction: Transaction,
    principal_id: str,
    *,
    source_event_id: str,
    question_event_id: str,
    selection_key: str,
    creator_agent: str,
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
    if stored_selection is not None:
        return (
            stored_selection
            if stored_selection.question_event_id == question_event_id
            and stored_selection.selection_key == selection_key
            else None
        )
    if source["semantic_consumer"] is not None:
        return None
    question_row = _question_row(transaction, principal_id, question_event_id)
    if (
        question_row is None
        or question_row["room_id"] != room_id
        or int(question_row["membership_epoch"]) != membership_epoch
        or question_row["creator_agent"] != creator_agent
    ):
        return None
    selection = _selection_from_row(question_row, selection_key)
    if selection is None:
        return None

    transaction.execute(
        """
        UPDATE journal_events
        SET semantic_consumer = ?
        WHERE principal_id = ? AND event_id = ?
        """,
        (SemanticConsumer.INTERACTIVE_REACTION.value, principal_id, source_event_id),
    )
    _store_selection(transaction, principal_id, source_event_id, selection)
    return selection


def claim_text(  # noqa: PLR0911 - each failed ownership predicate is terminal
    transaction: Transaction,
    principal_id: str,
    *,
    source_event_id: str,
    selection_key: str,
    creator_agent: str,
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
    if stored_selection is not None:
        return stored_selection if stored_selection.selection_key == selection_key else None

    candidate_question = transaction.fetchone(
        """
        SELECT question_event_id
        FROM interactive_questions
        WHERE principal_id = ? AND room_id = ? AND thread_id = ?
          AND creator_agent = ? AND membership_epoch = ?
        ORDER BY created_at_ns, question_event_id/*bytes*/
        LIMIT 1
        """,
        (principal_id, room_id, stored_thread_id, creator_agent, membership_epoch),
    )
    if candidate_question is None:
        return None
    question_row = _question_row(transaction, principal_id, str(candidate_question["question_event_id"]))
    if (
        question_row is None
        or question_row["room_id"] != room_id
        or decode_thread_id(str(question_row["thread_id"])) != thread_id
        or int(question_row["membership_epoch"]) != membership_epoch
        or question_row["creator_agent"] != creator_agent
    ):
        return None
    selection = _selection_from_row(question_row, selection_key)
    if selection is None:
        return None
    _store_selection(transaction, principal_id, source_event_id, selection)
    return selection


def register_if_current(
    transaction: Transaction,
    principal_id: str,
    *,
    expected_membership_epoch: int,
    question: InteractiveQuestion,
) -> bool:
    """Register one question only while its captured membership is active."""
    if not reads.claim_membership_epoch(
        transaction,
        principal_id,
        room_id=question.room_id,
        expected_membership_epoch=expected_membership_epoch,
    ):
        return False
    question_json = _question_json(question)
    transaction.execute(
        """
        INSERT INTO interactive_questions (
            principal_id, question_event_id, room_id, thread_id, creator_agent,
            question_json, membership_epoch, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (principal_id, question_event_id) DO NOTHING
        """,
        (
            principal_id,
            question.question_event_id,
            question.room_id,
            encode_thread_id(question.thread_id),
            question.creator_agent,
            question_json,
            expected_membership_epoch,
            time.time_ns(),
        ),
    )
    stored = transaction.fetchone(
        """
        SELECT question_event_id, room_id, thread_id, creator_agent, question_json, membership_epoch
        FROM interactive_questions
        WHERE principal_id = ? AND question_event_id = ?
        """,
        (principal_id, question.question_event_id),
    )
    if stored is None:
        msg = f"Interactive question {question.question_event_id!r} disappeared during registration"
        raise RuntimeError(msg)
    if (
        stored["room_id"] != question.room_id
        or decode_thread_id(str(stored["thread_id"])) != question.thread_id
        or stored["creator_agent"] != question.creator_agent
        or stored["question_json"] != question_json
        or int(stored["membership_epoch"]) != expected_membership_epoch
    ):
        msg = f"Interactive question {question.question_event_id!r} changed across registration replay"
        raise ValueError(msg)
    return True


def forget(transaction: Transaction, principal_id: str, question_event_id: str) -> None:
    """Forget one active question whose Matrix message no longer offers it."""
    transaction.execute(
        "DELETE FROM interactive_questions WHERE principal_id = ? AND question_event_id = ?",
        (principal_id, question_event_id),
    )
