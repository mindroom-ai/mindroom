"""Determinism and credential-safety tests for the manual cache audit harness."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
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
    ExpectationValidation,
    InteractionRecord,
    MatrixApi,
    MatrixAuditError,
    ThreadReadRecord,
    _begin_readonly_snapshot,
    _parse_args,
    _secret_free_evidence,
    _strict_thread_read_sequence,
    _wait_for_cache_edit_index,
    media_fixtures,
    new_transaction_id,
    run_audit,
    validate_interaction_expectations,
    validate_media_fixtures,
    write_evidence,
)

if TYPE_CHECKING:
    from pathlib import Path


def _empty_evidence() -> AuditEvidence:
    return AuditEvidence(
        schema_version=1,
        generated_at="2026-07-18T00:00:00+00:00",
        homeserver="https://matrix.example",
        user_id="@audit:example",
        joined_members=("@audit:example",),
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


def test_media_fixtures_are_small_and_byte_stable() -> None:
    """Every embedded fixture should remain tiny and byte-stable."""
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


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed")
def test_media_fixtures_are_decodable() -> None:
    """The complete fixture set should pass the harness's real decoders."""
    validate_media_fixtures(media_fixtures())


def test_media_validation_reports_missing_ffprobe_cleanly() -> None:
    """A host outside the dev shell should receive an actionable dependency error."""
    with (
        patch(
            "tests.manual.matrix_event_cache_live_audit.subprocess.run",
            side_effect=FileNotFoundError,
        ),
        pytest.raises(MatrixAuditError, match="ffprobe is required"),
    ):
        validate_media_fixtures(media_fixtures())


def test_transaction_ids_are_unique_uuids() -> None:
    """Every idempotent Matrix write should receive a fresh UUID."""
    transaction_ids = {new_transaction_id() for _ in range(100)}

    assert len(transaction_ids) == 100
    assert all(str(UUID(transaction_id)) == transaction_id for transaction_id in transaction_ids)


def test_readonly_snapshot_is_consistent_across_concurrent_writes(tmp_path: Path) -> None:
    """Service-cache evidence queries should share one stable read transaction."""
    database_path = tmp_path / "service.db"
    database_uri = f"file:{database_path}?mode=ro"
    with closing(sqlite3.connect(database_path)) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("CREATE TABLE evidence (event_id TEXT PRIMARY KEY)")
        writer.execute("INSERT INTO evidence VALUES ('$before')")
        writer.commit()
        with closing(sqlite3.connect(database_uri, uri=True)) as reader:
            _begin_readonly_snapshot(reader)
            assert reader.in_transaction is True
            assert reader.execute("PRAGMA query_only").fetchone() == (1,)
            assert reader.execute("SELECT event_id FROM evidence").fetchall() == [("$before",)]

            writer.execute("INSERT INTO evidence VALUES ('$after')")
            writer.commit()

            assert reader.execute("SELECT event_id FROM evidence").fetchall() == [("$before",)]


@pytest.mark.asyncio
async def test_edit_redaction_wait_uses_readonly_service_observation(tmp_path: Path) -> None:
    """The harness should observe the dependent edit before asking Matrix to redact its original."""
    database_path = tmp_path / "service.db"
    with closing(sqlite3.connect(database_path)) as db:
        db.execute("CREATE TABLE event_edits (room_id TEXT, edit_event_id TEXT)")
        db.execute("INSERT INTO event_edits VALUES ('!audit:example', '$edit')")
        db.commit()

    await _wait_for_cache_edit_index(
        database_path,
        room_id="!audit:example",
        edit_event_id="$edit",
        timeout_seconds=0.0,
    )
    with pytest.raises(MatrixAuditError, match="did not observe edit"):
        await _wait_for_cache_edit_index(
            database_path,
            room_id="!audit:example",
            edit_event_id="$missing",
            timeout_seconds=0.0,
        )


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


def test_evidence_writer_refuses_unvalidated_output(tmp_path: Path) -> None:
    """Durable audit output should never be written without full expectation validation."""
    evidence_path = tmp_path / "evidence.json"
    config = AuditConfig(
        base_url="https://matrix.example",
        access_token=UUID("10000000-0000-4000-8000-000000000005").hex,
        invite_access_token=None,
        evidence_path=evidence_path,
        cache_db_path=tmp_path / "service.db",
        strict_read_cache_db_path=tmp_path / "strict.db",
        invite_user_id=None,
        trigger_user_id=None,
        strict_thread_reads=True,
        settle_seconds=0.0,
        trigger_wait_seconds=0.0,
    )

    with pytest.raises(MatrixAuditError, match="requires complete passing"):
        write_evidence(_empty_evidence(), config)

    assert evidence_path.exists() is False

    validated = replace(
        _empty_evidence(),
        accounting_missing_event_ids=("$missing",),
        expectation_validation=ExpectationValidation(
            status="passed",
            interaction_records=1,
            assertions=1,
            strict_read_cache_isolated=True,
        ),
    )
    with pytest.raises(MatrixAuditError, match="complete homeserver-to-cache accounting"):
        write_evidence(validated, config)

    with pytest.raises(MatrixAuditError, match="complete homeserver-to-cache accounting"):
        write_evidence(
            replace(
                validated,
                accounting_missing_event_ids=(),
                cache_only_event_ids=("$cache-only",),
            ),
            config,
        )

    assert evidence_path.exists() is False


@pytest.mark.asyncio
async def test_audit_refuses_an_unverified_invited_account(tmp_path: Path) -> None:
    """An invited audit identity must be authenticated before room creation."""
    config = AuditConfig(
        base_url="https://matrix.example",
        access_token=UUID("10000000-0000-4000-8000-000000000006").hex,
        invite_access_token=None,
        evidence_path=tmp_path / "evidence.json",
        cache_db_path=tmp_path / "service.db",
        strict_read_cache_db_path=tmp_path / "strict.db",
        invite_user_id="@unverified:example",
        trigger_user_id=None,
        strict_thread_reads=True,
        settle_seconds=0.0,
        trigger_wait_seconds=0.0,
    )

    with pytest.raises(MatrixAuditError, match="requires both its user ID and access token"):
        await run_audit(config)


@pytest.mark.asyncio
async def test_audit_verifies_invite_token_before_room_creation(tmp_path: Path) -> None:
    """A mismatched token must fail before createRoom can expose the private room."""
    owner_token = UUID("10000000-0000-4000-8000-000000000008").hex
    invite_token = UUID("10000000-0000-4000-8000-000000000009").hex
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.url.path.endswith("/account/whoami")
        if request.headers["Authorization"] == f"Bearer {owner_token}":
            return httpx.Response(200, json={"device_id": "OWNER", "user_id": "@owner:example"})
        if request.headers["Authorization"] == f"Bearer {invite_token}":
            return httpx.Response(200, json={"device_id": "OTHER", "user_id": "@other:example"})
        msg = "Unexpected access token"
        raise AssertionError(msg)

    config = AuditConfig(
        base_url="https://matrix.example",
        access_token=owner_token,
        invite_access_token=invite_token,
        evidence_path=tmp_path / "evidence.json",
        cache_db_path=tmp_path / "service.db",
        strict_read_cache_db_path=tmp_path / "strict.db",
        invite_user_id="@invited:example",
        trigger_user_id=None,
        strict_thread_reads=True,
        settle_seconds=0.0,
        trigger_wait_seconds=0.0,
    )

    with (
        patch("tests.manual.matrix_event_cache_live_audit.validate_media_fixtures"),
        pytest.raises(MatrixAuditError, match="does not belong"),
    ):
        await run_audit(config, transport=httpx.MockTransport(handler))

    assert len(requested_paths) == 2
    assert all("/createRoom" not in path for path in requested_paths)


@pytest.mark.asyncio
async def test_audit_rejects_unexpected_private_room_membership_before_event_work(
    tmp_path: Path,
) -> None:
    """Unexpected joined members must abort the audit before media or event writes."""
    owner_token = UUID("10000000-0000-4000-8000-000000000010").hex
    requested_paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        assert request.headers["Authorization"] == f"Bearer {owner_token}"
        if request.url.path.endswith("/account/whoami"):
            return httpx.Response(200, json={"device_id": "OWNER", "user_id": "@owner:example"})
        if request.url.path.endswith("/createRoom"):
            return httpx.Response(200, json={"room_id": "!audit:example"})
        if request.url.path.endswith("/joined_members"):
            return httpx.Response(
                200,
                json={
                    "joined": {
                        "@intruder:example": {},
                        "@owner:example": {},
                    },
                },
            )
        msg = f"Unexpected request after membership verification: {request.url.path}"
        raise AssertionError(msg)

    config = AuditConfig(
        base_url="https://matrix.example",
        access_token=owner_token,
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
        patch("tests.manual.matrix_event_cache_live_audit.validate_media_fixtures"),
        pytest.raises(MatrixAuditError, match="joined membership differs"),
    ):
        await run_audit(config, transport=httpx.MockTransport(handler))

    assert requested_paths == [
        "/_matrix/client/v3/account/whoami",
        "/_matrix/client/v3/createRoom",
        "/_matrix/client/v3/rooms/!audit:example/joined_members",
    ]


@pytest.mark.asyncio
async def test_matrix_api_records_authenticated_joined_members_in_sorted_order() -> None:
    """Raw evidence membership should come from the authenticated Matrix endpoint."""
    access_token = UUID("10000000-0000-4000-8000-000000000011").hex

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert request.url.path == "/_matrix/client/v3/rooms/!audit:example/joined_members"
        return httpx.Response(
            200,
            json={
                "joined": {
                    "@zeta:example": {},
                    "@alpha:example": {},
                },
            },
        )

    async with MatrixApi(
        base_url="https://matrix.example",
        access_token=access_token,
        transport=httpx.MockTransport(handler),
    ) as api:
        joined_members = await api.joined_members("!audit:example")

    assert joined_members == ("@alpha:example", "@zeta:example")
    assert [timing.operation for timing in api.timings] == ["joined_members"]


def test_cli_requires_invited_user_and_token_environment_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command line cannot invite an identity that the harness cannot authenticate."""
    monkeypatch.setenv(
        "MATRIX_ACCESS_TOKEN",
        UUID("10000000-0000-4000-8000-000000000007").hex,
    )
    with (
        patch(
            "sys.argv",
            [
                "matrix_event_cache_live_audit.py",
                "--evidence",
                str(tmp_path / "evidence.json"),
                "--cache-db",
                str(tmp_path / "service.db"),
                "--strict-read-cache-db",
                str(tmp_path / "strict.db"),
                "--strict-thread-reads",
                "--invite-user-id",
                "@unverified:example",
            ],
        ),
        pytest.raises(SystemExit, match="2"),
    ):
        _parse_args()

    assert "must be provided together" in capsys.readouterr().err


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
        InteractionRecord(
            family="strict_read_rejection_target",
            event_type="m.room.message",
            event_id="$strict-child",
            expected_point_cache=False,
            expected_representation="tombstone",
            expected_visible_thread_history=True,
        ),
    )
    cache = CacheSnapshot(
        active_event_ids=("$child",),
        tombstoned_event_ids=("$strict-child", "$target"),
        edit_event_ids=(),
        event_thread_ids=("$child",),
        thread_state_rows=1,
        orphan_edit_rows=0,
        orphan_thread_rows=0,
        quick_check="ok",
    )
    reads = (
        _thread_read(
            1,
            source="homeserver",
            visible_event_ids=("$child", "$strict-child"),
            cache_reject_reason="no_cache_state",
        ),
        _thread_read(2, source="cache", visible_event_ids=("$child", "$strict-child")),
        _thread_read(
            3,
            source="homeserver",
            visible_event_ids=("$child",),
            cache_reject_reason="thread_invalidated_after_validation",
        ),
    )
    validation = validate_interaction_expectations(
        records,
        homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
        homeserver_redaction_event_ids=("$redaction",),
        cache=cache,
        accounting_missing_event_ids=(),
        cache_only_event_ids=(),
        thread_reads=reads,
    )

    assert validation.status == "passed"
    assert validation.interaction_records == 4
    assert validation.assertions > 20
    with pytest.raises(MatrixAuditError, match=r"thread_child\.point_cache"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=replace(cache, active_event_ids=()),
            accounting_missing_event_ids=(),
            cache_only_event_ids=(),
            thread_reads=reads,
        )

    with pytest.raises(MatrixAuditError, match=r"accounting\.missing_event_ids"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=("$unrepresented",),
            cache_only_event_ids=(),
            thread_reads=reads,
        )

    with pytest.raises(MatrixAuditError, match=r"accounting\.cache_only_event_ids"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=(),
            cache_only_event_ids=("$cache-only",),
            thread_reads=reads,
        )

    corrupt_second_read = replace(
        reads[1],
        visible_event_ids=("$child",),
    )
    with pytest.raises(MatrixAuditError, match=r"strict_reads\.second\.visible_event_ids"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=(),
            cache_only_event_ids=(),
            thread_reads=(reads[0], corrupt_second_read, reads[2]),
        )

    with pytest.raises(MatrixAuditError, match=r"strict_reads\.second\.homeserver_fetch_ms"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=(),
            cache_only_event_ids=(),
            thread_reads=(reads[0], replace(reads[1], homeserver_fetch_ms=1.0), reads[2]),
        )

    with pytest.raises(MatrixAuditError, match=r"strict_reads\.third\.cache_reject_reason"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=(),
            cache_only_event_ids=(),
            thread_reads=(reads[0], reads[1], replace(reads[2], cache_reject_reason="other")),
        )

    with pytest.raises(MatrixAuditError, match=r"strict_reads\.third\.redacted_event_absent"):
        validate_interaction_expectations(
            records,
            homeserver_event_ids=("$child", "$target", "$redaction", "$strict-child"),
            homeserver_redaction_event_ids=("$redaction",),
            cache=cache,
            accounting_missing_event_ids=(),
            cache_only_event_ids=(),
            thread_reads=(
                reads[0],
                reads[1],
                replace(
                    reads[2],
                    visible_event_ids=("$child", "$strict-child"),
                ),
            ),
        )
