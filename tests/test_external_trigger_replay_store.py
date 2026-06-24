"""Tests for external trigger payloads and replay storage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.external_triggers.models import ExternalTriggerAcceptedResponse, ExternalTriggerPayload
from mindroom.external_triggers.replay_store import (
    ExternalTriggerEventClaim,
    ExternalTriggerReplayStore,
    ExternalTriggerReplayStoreError,
)

if TYPE_CHECKING:
    from pathlib import Path


def _store_path(tmp_path: Path) -> Path:
    path = tmp_path / "external_triggers" / "replay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_payload_rejects_target_override_fields_and_uses_isolated_data_dict() -> None:
    """Payloads should not accept target override fields and should not share data defaults."""
    with pytest.raises(ValidationError, match="room_id"):
        ExternalTriggerPayload.model_validate(
            {
                "kind": "campground.availability",
                "message": "Site opened",
                "room_id": "!unsafe:example.org",
            },
        )

    first = ExternalTriggerPayload(kind="campground.availability", message="Site opened")
    second = ExternalTriggerPayload(kind="campground.availability", message="Different site opened")
    first.data["site"] = "42"

    assert first.kind == "campground.availability"
    assert first.message == "Site opened"
    assert first.event_id is None
    assert first.title is None
    assert first.data == {"site": "42"}
    assert second.data == {}


@pytest.mark.parametrize(
    ("field_name", "payload"),
    [
        ("kind", {"kind": "  ", "message": "Site opened"}),
        ("message", {"kind": "campground.availability", "message": "  "}),
    ],
)
def test_payload_rejects_blank_kind_and_message(field_name: str, payload: dict[str, str]) -> None:
    """Payload kind and message must contain non-whitespace text."""
    with pytest.raises(ValidationError, match=field_name):
        ExternalTriggerPayload.model_validate(payload)


def test_accepted_response_defaults_matrix_event_id_and_duplicate_flag() -> None:
    """Accepted responses should expose stable duplicate and delivery fields."""
    response = ExternalTriggerAcceptedResponse(
        accepted=True,
        trigger_id="campground",
        event_id="availability-123",
    )

    assert response.accepted is True
    assert response.duplicate is False
    assert response.trigger_id == "campground"
    assert response.event_id == "availability-123"
    assert response.matrix_event_id is None


def test_shared_store_instances_coordinate_nonce_and_event_claims(tmp_path: Path) -> None:
    """Store instances for one tracking root should share replay state."""
    first_store = ExternalTriggerReplayStore(tmp_path)
    second_store = ExternalTriggerReplayStore(tmp_path)

    assert first_store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert not second_store.claim_nonce("campground", "nonce-1", now=1_001, ttl_seconds=300)

    assert first_store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )
    assert second_store.claim_event_id("campground", "availability-123", now=1_001, ttl_seconds=300) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )

    second_store.mark_event_delivered("campground", "availability-123", now=1_002, ttl_seconds=300)

    assert first_store.claim_event_id("campground", "availability-123", now=1_003, ttl_seconds=300) is (
        ExternalTriggerEventClaim.DELIVERED
    )


def test_release_after_send_failure_keeps_nonce_single_use_but_allows_event_retry(tmp_path: Path) -> None:
    """Rollback release should remove only the event-id claim."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    store.release_event_id("campground", "availability-123")

    assert not store.claim_nonce("campground", "nonce-1", now=1_010, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_010, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


def test_expired_nonce_and_event_id_can_be_reclaimed(tmp_path: Path) -> None:
    """Expired replay claims should be pruned on the next claim."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


def test_in_progress_event_id_processing_ttl_outlives_nonce_ttl(tmp_path: Path) -> None:
    """Long-running deliveries should not be re-claimed when signature replay TTL expires."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=86_400) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=86_400) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )


def test_nonce_and_event_id_remain_claimed_at_exact_expiry_boundary(tmp_path: Path) -> None:
    """Replay claims should expire after their final valid timestamp."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    assert not store.claim_nonce("campground", "nonce-1", now=1_300, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_300, ttl_seconds=300) is (
        ExternalTriggerEventClaim.IN_PROGRESS
    )

    assert store.claim_nonce("campground", "nonce-1", now=1_301, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_301, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )


@pytest.mark.parametrize("store_payload", [["not", "a", "dict"], {"nonces": {}}])
def test_invalid_store_shape_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed top-level store structure should not reset replay protection."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


@pytest.mark.parametrize(
    "store_payload",
    [
        {"nonces": {"campground": {"nonce-1": {"expires_at": "later"}}}, "events": {}},
        {"nonces": {"campground": {"nonce-1": {}}}, "events": {}},
        {"nonces": {"campground": {"nonce-1": {"expires_at": True}}}, "events": {}},
        {"nonces": {"campground": ["nonce-1"]}, "events": {}},
    ],
)
def test_invalid_nested_nonce_record_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed nonce records should not be silently dropped."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


@pytest.mark.parametrize(
    "store_payload",
    [
        {
            "nonces": {},
            "events": {"campground": {"availability-123": {"state": "bad", "expires_at": 1_300, "delivered_at": None}}},
        },
        {
            "nonces": {},
            "events": {
                "campground": {
                    "availability-123": {"state": "delivered", "expires_at": "later", "delivered_at": 1_000},
                },
            },
        },
        {
            "nonces": {},
            "events": {
                "campground": {
                    "availability-123": {"state": "delivered", "expires_at": 1_300, "delivered_at": "bad"},
                },
            },
        },
        {
            "nonces": {},
            "events": {"campground": {"availability-123": {"state": "delivered", "expires_at": 1_300}}},
        },
        {"nonces": {}, "events": {"campground": ["availability-123"]}},
    ],
)
def test_invalid_nested_event_record_fails_closed(tmp_path: Path, store_payload: object) -> None:
    """Malformed event records should not be silently dropped."""
    store_path = _store_path(tmp_path)
    store_path.write_text(json.dumps(store_payload), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300)


def test_corrupt_json_store_fails_closed(tmp_path: Path) -> None:
    """Syntactically corrupt store JSON should not reset replay protection."""
    store_path = _store_path(tmp_path)
    store_path.write_text("{not valid json", encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    with pytest.raises(ExternalTriggerReplayStoreError, match="invalid"):
        store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
