"""Determinism and credential-safety tests for the manual cache audit harness."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from tests.manual.matrix_event_cache_live_audit import (
    AuditEvidence,
    MatrixApi,
    MatrixAuditError,
    _secret_free_evidence,
    media_fixtures,
    new_transaction_id,
)


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
