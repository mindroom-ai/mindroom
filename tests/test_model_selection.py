"""Strict model picker wire parsing and immutable acknowledgement checkpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal import turn_rows
from mindroom.message_target import MessageTarget
from mindroom.model_selection import MODEL_SELECTION_CONTENT_KEY, parse_model_selection
from mindroom.turn_record import TurnRecord, canonicalize_turn_record

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore

_TARGET = MessageTarget.resolve("!room:localhost", None, "$command")


def test_absent_metadata_preserves_text_commands() -> None:
    """Treating ordinary text as malformed would break existing commands."""
    assert parse_model_selection({"body": "!model reset"}) is None


@pytest.mark.parametrize("model", ["default", "reset", "clear", "list", "show"])
def test_explicit_set_preserves_alias_model_keys(model: str) -> None:
    """Text alias interpretation must not affect explicit operations."""
    request = parse_model_selection(
        {
            MODEL_SELECTION_CONTENT_KEY: {
                "version": 1,
                "runtime_user_id": "@router:localhost",
                "runtime_device_id": "DEVICE",
                "operation": "set",
                "model": model,
            },
        },
    )
    assert request is not None
    assert request.operation == "set"
    assert request.model == model


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        [],
        {"version": True, "runtime_user_id": "@router:localhost", "runtime_device_id": "DEVICE", "operation": "reset"},
        {"version": 2, "runtime_user_id": "@router:localhost", "runtime_device_id": "DEVICE", "operation": "reset"},
        {"version": 1, "runtime_user_id": "@router:localhost", "runtime_device_id": "DEVICE", "operation": "set"},
        {
            "version": 1,
            "runtime_user_id": "@router:localhost",
            "runtime_device_id": "DEVICE",
            "operation": "reset",
            "model": None,
        },
        {"version": 1, "runtime_user_id": "@router:localhost", "runtime_device_id": "DEVICE", "operation": "clear"},
        {
            "version": 1,
            "runtime_user_id": "@router:localhost",
            "runtime_device_id": "DEVICE",
            "operation": "set",
            "model": "x" * 1025,
        },
        {
            "version": 1,
            "runtime_user_id": "@router:localhost",
            "runtime_device_id": "DEVICE",
            "operation": "reset",
            "sender": "@admin:localhost",
        },
    ],
)
def test_malformed_metadata_never_becomes_text_fallback(metadata: object) -> None:
    """Conflating invalid and absent would permit unintended textual mutation."""
    with pytest.raises(ValueError, match="Invalid structured"):
        parse_model_selection({MODEL_SELECTION_CONTENT_KEY: metadata})


def test_command_checkpoint_copies_freezes_and_filters_metadata() -> None:
    """Caller mutation or standard content keys cannot alter durable acknowledgements."""
    result = {"version": 1, "status": "applied", "override": "default"}
    record = TurnRecord.create(
        ["$command"],
        command_result_text="Applied",
        command_result_extra_content={
            "io.mindroom.model_selection_result": result,
            "body": "injected",
            "m.relates_to": {},
        },
    )
    result["override"] = "changed"
    assert record.command_result_extra_content == {
        "io.mindroom.model_selection_result": {"version": 1, "status": "applied", "override": "default"},
    }
    with pytest.raises(TypeError):
        record.command_result_extra_content["io.mindroom.model_selection_result"]["override"] = "changed"
    assert canonicalize_turn_record(record, command_result_text=None).command_result_extra_content is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [None, {"io.mindroom.model_selection_result": {"status": "applied", "override": "default"}}],
)
async def test_checkpoint_round_trips_through_turn_rows(
    journal_store: EventJournalStore,
    metadata: dict | None,
) -> None:
    """A command result and its acknowledgement metadata survive storage, and a result without metadata stays bare."""
    record = TurnRecord.create(
        ["$command"],
        completed=False,
        command_result_text="saved",
        command_result_extra_content=metadata,
        conversation_target=_TARGET,
    )
    await journal_store.backend.write(
        lambda transaction: turn_rows.record_pending_turn(transaction, "agent", record, room_id=_TARGET.room_id),
    )
    restored = await journal_store.backend.read(
        lambda transaction: turn_rows.get_turn_record(transaction, "agent", "$command"),
    )
    assert restored is not None
    assert restored.command_result_text == "saved"
    assert restored.command_result_extra_content == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("with_metadata", [False, True])
async def test_a_stored_result_keeps_its_text_and_metadata_together(
    journal_store: EventJournalStore,
    *,
    with_metadata: bool,
) -> None:
    """The first checkpointed result is kept whole: a later result never pairs its text with the earlier metadata."""
    older = TurnRecord.create(
        ["$command"],
        completed=False,
        command_result_text="old applied",
        command_result_extra_content={
            "io.mindroom.model_selection_result": {"status": "applied", "override": "default"},
        },
        conversation_target=_TARGET,
    )
    metadata = {"io.mindroom.model_selection_result": {"status": "rejected", "error": "new"}} if with_metadata else None
    for record in (
        older,
        canonicalize_turn_record(older, command_result_text="new result", command_result_extra_content=metadata),
    ):
        await journal_store.backend.write(
            lambda transaction, record=record: turn_rows.record_pending_turn(
                transaction,
                "agent",
                record,
                room_id=_TARGET.room_id,
            ),
        )
    stored = await journal_store.backend.read(
        lambda transaction: turn_rows.get_turn_record(transaction, "agent", "$command"),
    )
    assert stored is not None
    assert stored.command_result_text == "old applied"
    assert stored.command_result_extra_content == older.command_result_extra_content


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_user_id", ""),
        ("runtime_user_id", "x" * 1025),
        ("runtime_device_id", ""),
        ("runtime_device_id", "x" * 256),
        ("runtime_device_id", None),
        ("model", ""),
    ],
)
def test_invalid_target_or_model_string_is_rejected(field: str, value: object) -> None:
    """Missing or oversized identity/model strings must not enter command execution."""
    raw = {
        "version": 1,
        "runtime_user_id": "@router:localhost",
        "runtime_device_id": "DEVICE",
        "operation": "set",
        "model": "default",
    }
    raw[field] = value
    with pytest.raises(ValueError, match="Invalid structured"):
        parse_model_selection({MODEL_SELECTION_CONTENT_KEY: raw})
