"""Strict model picker wire parsing and immutable acknowledgement checkpoints."""

from __future__ import annotations

import json

import pytest

from mindroom.handled_turns import TurnRecordCodec, _merge_same_identity_records
from mindroom.model_selection import MODEL_SELECTION_CONTENT_KEY, parse_model_selection
from mindroom.turn_record import TurnRecord, canonicalize_turn_record
from mindroom.turn_store import _backfill_missing_turn_facts


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


@pytest.mark.parametrize(
    "metadata",
    [None, {"io.mindroom.model_selection_result": {"status": "applied", "override": "default"}}],
)
def test_checkpoint_codec_roundtrip_and_legacy_missing_metadata(metadata: dict | None) -> None:
    """Old records remain readable and new acknowledgements survive serialization."""
    record = TurnRecord.create(["$command"], command_result_text="saved", command_result_extra_content=metadata)
    raw = json.loads(json.dumps(TurnRecordCodec._to_ledger_record(record)))
    restored = TurnRecordCodec._from_ledger_record("$command", raw)
    assert restored is not None
    assert restored.command_result_text == "saved"
    assert restored.command_result_extra_content == metadata


@pytest.mark.parametrize("with_metadata", [False, True])
def test_result_merges_keep_text_and_metadata_from_same_snapshot(*, with_metadata: bool) -> None:
    """A newer plain/uncertain result must never inherit older applied metadata."""
    older = TurnRecord.create(
        ["$command"],
        timestamp=1,
        command_result_text="old applied",
        command_result_extra_content={
            "io.mindroom.model_selection_result": {"status": "applied", "override": "default"},
        },
    )
    metadata = {"io.mindroom.model_selection_result": {"status": "rejected", "error": "new"}} if with_metadata else None
    newer = TurnRecord.create(
        ["$command"],
        timestamp=2,
        command_result_text="new result",
        command_result_extra_content=metadata,
    )
    for merged in (_merge_same_identity_records(newer, older), _backfill_missing_turn_facts(newer, older)):
        assert merged.command_result_text == "new result"
        assert merged.command_result_extra_content == metadata


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
