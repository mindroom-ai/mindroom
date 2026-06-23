"""Tests for external trigger payloads and replay storage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.external_triggers.models import ExternalTriggerAcceptedResponse, ExternalTriggerPayload
from mindroom.external_triggers.replay_store import ExternalTriggerEventClaim, ExternalTriggerReplayStore

if TYPE_CHECKING:
    from pathlib import Path


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


def test_release_after_send_failure_allows_nonce_and_event_id_to_be_claimed_again(tmp_path: Path) -> None:
    """Rollback release should remove nonce and event-id claims."""
    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
    assert store.claim_event_id("campground", "availability-123", now=1_000, ttl_seconds=300) is (
        ExternalTriggerEventClaim.FRESH
    )

    store.release_nonce("campground", "nonce-1")
    store.release_event_id("campground", "availability-123")

    assert store.claim_nonce("campground", "nonce-1", now=1_010, ttl_seconds=300)
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


def test_invalid_store_shape_resets_to_empty(tmp_path: Path) -> None:
    """Malformed top-level store structure should not break replay checks."""
    store_path = tmp_path / "external_triggers.json"
    store_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)


def test_corrupt_json_store_resets_to_empty(tmp_path: Path) -> None:
    """Syntactically corrupt store JSON should not break replay checks."""
    store_path = tmp_path / "external_triggers.json"
    store_path.write_text("{not valid json", encoding="utf-8")

    store = ExternalTriggerReplayStore(tmp_path)

    assert store.claim_nonce("campground", "nonce-1", now=1_000, ttl_seconds=300)
