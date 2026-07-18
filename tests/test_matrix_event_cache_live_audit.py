"""Determinism and credential-safety tests for the manual cache audit harness."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest

from tests.manual.matrix_event_cache_live_audit import (
    AuditConfig,
    AuditEvidence,
    CacheSnapshot,
    InteractionRecord,
    MatrixApi,
    MatrixAuditError,
    ThreadReadRecord,
    _secret_free_evidence,
    _strict_thread_read_sequence,
    media_fixtures,
    new_transaction_id,
    validate_interaction_expectations,
)

if TYPE_CHECKING:
    from pathlib import Path


def _empty_evidence() -> AuditEvidence:
    return AuditEvidence(
        schema_version=1,
        generated_at="2026-07-18T00:00:00+00:00",
        homeserver="https://matrix.example",
        user_id="@audit:example",
        room_id="!audit:example",
        thread_root_id="$root",
        interactions=(),
        media=(),
        request_timings=(),
        homeserver_event_ids=(),
        homeserver_redaction_event_ids=(),
        cache=None,
        accounting_missing_event_ids=(),
        cache_only_event_ids=(),
        trigger_event_ids=(),
        thread_reads=(),
        expectation_validation=None,
        notes=(),
    )


def test_media_fixtures_are_small_stable_and_decodable() -> None:
    """Every embedded fixture should remain tiny, byte-stable, and decodable."""
    fixtures = media_fixtures()

    assert {fixture.filename: (fixture.mime_type, len(fixture.payload), fixture.sha256) for fixture in fixtures} == {
        "black.webm": (
            "video/webm",
            522,
            "6aedcc50ca4eeed45b81eb6ab1c82d445b7a9941652eb9fca9148935cdfab5e4",
        ),
        "silence.wav": (
            "audio/wav",
            364,
            "5341a0da3824f5be899ff8ba691f9bf28b9702de7c27752043c69e60a96ffa1c",
        ),
        "tiny.png": (
            "image/png",
            68,
            "431ced6916a2a21a156e38701afe55bbd7f88969fbbfc56d7fe099d47f265460",
        ),
        "tiny.txt": (
            "text/plain",
            28,
            "f8529cbbaa1403b3c5a2992e85056df953aa85c1a3e3d6cfbded9444a9f52d45",
        ),
    }


def test_transaction_ids_are_unique_uuids() -> None:
    """Every idempotent Matrix write should receive a fresh UUID."""
    transaction_ids = {new_transaction_id() for _ in range(100)}

    assert len(transaction_ids) == 100
    assert all(str(UUID(transaction_id)) == transaction_id for transaction_id in transaction_ids)


def test_evidence_rejects_secret_keys_and_values() -> None:
    """Sanitized evidence should reject both secret-shaped keys and token values."""
    evidence = _empty_evidence()
    secret = UUID("10000000-0000-4000-8000-000000000001").hex

    assert _secret_free_evidence(evidence, access_tokens=(secret,))["room_id"] == "!audit:example"
    with pytest.raises(MatrixAuditError, match="access-token value"):
        _secret_free_evidence(
            replace(evidence, notes=(f"leak: {secret}",)),
            access_tokens=(secret,),
        )
    with pytest.raises(MatrixAuditError, match="forbidden secret key"):
        _secret_free_evidence(
            replace(evidence, media=({"access_token": "redacted"},)),
            access_tokens=(secret,),
        )


@pytest.mark.asyncio
async def test_authenticated_media_round_trip_keeps_token_out_of_evidence() -> None:
    """The harness should authenticate media transfer without retaining its token."""
    fixture = media_fixtures()[1]
    secret = UUID("10000000-0000-4000-8000-000000000002").hex

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        if request.url.path.endswith("/upload"):
            assert await request.aread() == fixture.payload
            return httpx.Response(200, json={"content_uri": "mxc://matrix.example/media-id"})
        if "/media/download/" in request.url.path:
            return httpx.Response(200, content=fixture.payload)
        raise AssertionError(request.url.path)

    async with MatrixApi(
        base_url="https://matrix.example",
        access_token=secret,
        transport=httpx.MockTransport(handler),
    ) as api:
        content_uri = await api.upload(fixture)
        downloaded = await api.download(content_uri, filename=fixture.filename)

    assert downloaded == fixture.payload
    assert [timing.operation for timing in api.timings] == [
        "upload:tiny.png",
        "download:tiny.png",
    ]
    assert secret not in repr(api.timings)


@pytest.mark.asyncio
async def test_matrix_api_rejects_malformed_json_without_exposing_body() -> None:
    """Malformed upstream JSON should fail without copying the response body into the error."""
    sensitive_body = "not-json-sensitive-upstream-content"
    access_token = UUID("10000000-0000-4000-8000-000000000003").hex

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sensitive_body)

    async with MatrixApi(
        base_url="https://matrix.example",
        access_token=access_token,
        transport=httpx.MockTransport(handler),
    ) as api:
        with pytest.raises(MatrixAuditError, match="returned malformed JSON") as exc_info:
            await api.whoami()

    assert sensitive_body not in str(exc_info.value)


@pytest.mark.asyncio
async def test_strict_read_closes_resources_when_cache_initialization_fails(
    tmp_path: Path,
) -> None:
    """Both isolated resources should close when cache initialization fails."""
    cache = AsyncMock()
    cache.initialize.side_effect = RuntimeError("initialization failed")
    client = AsyncMock()
    access_token = UUID("10000000-0000-4000-8000-000000000004").hex
    config = AuditConfig(
        base_url="https://matrix.example",
        access_token=access_token,
        invite_access_token=None,
        evidence_path=tmp_path / "evidence.json",
        cache_db_path=tmp_path / "service.db",
        strict_read_cache_db_path=tmp_path / "strict.db",
        invite_user_id=None,
        trigger_user_id=None,
        strict_thread_reads=True,
        settle_seconds=0.0,
        trigger_wait_seconds=0.0,
    )

    with (
        patch(
            "tests.manual.matrix_event_cache_live_audit.SqliteEventCache",
            return_value=cache,
        ),
        patch(
            "tests.manual.matrix_event_cache_live_audit.nio.AsyncClient",
            return_value=client,
        ),
        pytest.raises(RuntimeError, match="initialization failed"),
    ):
        await _strict_thread_read_sequence(
            AsyncMock(),
            config=config,
            room_id="!audit:example",
            root_id="$root",
            user_id="@audit:example",
            device_id="DEVICE",
            records=[],
        )

    client.close.assert_awaited_once()
    cache.close.assert_awaited_once()


def _thread_read(
    sequence: int,
    *,
    source: str,
    visible_event_ids: tuple[str, ...],
    cache_reject_reason: str | None = None,
) -> ThreadReadRecord:
    return ThreadReadRecord(
        sequence=sequence,
        mode="cache_hit" if source == "cache" else "full_scan",
        source=source,
        elapsed_ms=1.0,
        cache_read_ms=0.1,
        homeserver_fetch_ms=0.9 if source == "homeserver" else 0.0,
        homeserver_scan_pages=1 if source == "homeserver" else 0,
        homeserver_scanned_event_count=len(visible_event_ids) if source == "homeserver" else 0,
        visible_event_count=len(visible_event_ids),
        visible_event_ids=visible_event_ids,
        cache_reject_reason=cache_reject_reason,
        degraded=False,
        error=None,
    )


def test_interaction_expectations_are_executable_and_fail_closed() -> None:
    """Declared live expectations should be compared against every observed cache surface."""
    records = (
        InteractionRecord(
            family="thread_child",
            event_type="m.room.message",
            event_id="$child",
            expected_visible_thread_history=True,
            expected_event_thread_mapping=True,
            expected_room_level=False,
        ),
        InteractionRecord(
            family="redacted_target",
            event_type="m.reaction",
            event_id="$target",
            expected_point_cache=False,
            expected_representation="tombstone",
        ),
        InteractionRecord(
            family="redaction",
            event_type="m.room.redaction",
            event_id="$redaction",
            expected_point_cache=False,
            expected_representation="omitted",
        ),
    )
    cache = CacheSnapshot(
        active_event_ids=("$child",),
        tombstoned_event_ids=("$target",),
        edit_event_ids=(),
        event_thread_ids=("$child",),
        thread_state_rows=1,
        orphan_edit_rows=0,
        orphan_thread_rows=0,
        quick_check="ok",
    )
    reads = (
        _thread_read(1, source="homeserver", visible_event_ids=("$child",)),
        _thread_read(2, source="cache", visible_event_ids=("$child",)),
        _thread_read(
            3,
            source="homeserver",
            visible_event_ids=(),
            cache_reject_reason="thread_invalidated_after_validation",
        ),
    )

    validation = validate_interaction_expectations(
        records,
        homeserver_event_ids=("$child", "$target", "$redaction"),
        homeserver_redaction_event_ids=("$redaction",),
        cache=cache,
        thread_reads=reads,
    )

    assert validation.status == "passed"
    assert validation.interaction_records == 3
    assert validation.assertions > 20
    with pytest.raises(MatrixAuditError, match=r"thread_child\.point_cache"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=replace(cache, active_event_ids=()),
            thread_reads=reads,
        )
