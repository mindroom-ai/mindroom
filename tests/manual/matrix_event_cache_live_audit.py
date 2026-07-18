"""Create and verify a disposable Matrix event-cache interaction audit room.

The access token is read only from an environment variable and is sent only in
the HTTP Authorization header.
The emitted JSON contains IDs, event families, hashes, counts, and timings, but
never event bodies, credentials, headers, or raw Matrix responses.
"""

# ruff: noqa: D102, D105

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import sqlite3
import ssl
import struct
import subprocess
import tempfile
import time
import wave
import zlib
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import certifi
import httpx
import nio

from mindroom.matrix.cache import ThreadHistoryResult, thread_cache_rejection_reason
from mindroom.matrix.cache.sqlite_event_cache import SqliteEventCache
from mindroom.matrix.client_thread_history import fetch_dispatch_thread_snapshot

_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
_TINY_WEBM_BASE64 = (
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAHpEU2bdLpNu4tTq4QVSalmU6y"
    "BoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggElTbuMU6uEHFO7a1OsggHT7AEAAAAAAABZAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAV"
    "SalmsirXsYMPQkBNgI1MYXZmNjIuMTIuMTAyV0GNTGF2ZjYyLjEyLjEwMkSJiEBEAAAAAAAAFlSua8iuAQAAAAAA"
    "AD/XgQFzxYjAnWsiiPN80JyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhAJiWgDgkLCBArqBApqBAlW5gQESVMNn"
    "/HNzoGPAgGfImkWjh0VOQ09ERVJEh41MYXZmNjIuMTIuMTAyc3PWY8CLY8WIwJ1rIojzfNBnyKFFo4dFTkNPREVS"
    "RIeUTGF2YzYyLjI4LjEwMiBsaWJ2cHhnyKFFo4hEVVJBVElPTkSHkzAwOjAwOjAwLjA0MDAwMDAwMAAfQ7Z1qOeB"
    "AKOjgQAAgBACAJ0BKgIAAgAARwiFhYiFhIgCAgAMDWAA/v+rUIAcU7trkbuPs4EAt4r3gQHxggGm8IED"
)
_SECRET_KEY_FRAGMENTS = frozenset(
    {
        "access_token",
        "authorization",
        "jwt",
        "password",
        "registration_token",
        "refresh_token",
    },
)
_CACHE_OBSERVATION_TIMEOUT_SECONDS = 30.0


class MatrixAuditError(RuntimeError):
    """Raised when the disposable live audit cannot prove its next invariant."""


@dataclass(frozen=True, slots=True)
class AuditConfig:
    """Secret-safe runtime configuration for one disposable live audit."""

    base_url: str
    access_token: str = field(repr=False)
    invite_access_token: str | None = field(repr=False)
    evidence_path: Path
    cache_db_path: Path | None
    strict_read_cache_db_path: Path | None
    invite_user_id: str | None
    trigger_user_id: str | None
    strict_thread_reads: bool
    settle_seconds: float
    trigger_wait_seconds: float


@dataclass(frozen=True, slots=True)
class MediaFixture:
    """One deterministic, validated media payload."""

    filename: str
    mime_type: str
    payload: bytes = field(repr=False)

    @property
    def sha256(self) -> str:
        """Return the fixture digest recorded in evidence."""
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RequestTiming:
    """One secret-free Matrix request timing."""

    operation: str
    elapsed_ms: float
    status_code: int


@dataclass(frozen=True, slots=True)
class InteractionRecord:
    """Expected durable cache treatment for one emitted Matrix event."""

    family: str
    event_type: str
    event_id: str
    expected_point_cache: bool = True
    expected_visible_thread_history: bool = False
    expected_event_thread_mapping: bool = False
    expected_edit_index: bool = False
    expected_representation: str = "active"
    expected_room_level: bool = True


@dataclass(frozen=True, slots=True)
class CacheSnapshot:
    """Read-only SQLite cache evidence for the disposable room."""

    active_event_ids: tuple[str, ...]
    tombstoned_event_ids: tuple[str, ...]
    edit_event_ids: tuple[str, ...]
    event_thread_ids: tuple[str, ...]
    thread_state_rows: int
    orphan_edit_rows: int
    orphan_thread_rows: int
    quick_check: str


@dataclass(frozen=True, slots=True)
class ThreadReadRecord:
    """One strict thread-read result against the disposable audit cache."""

    sequence: int
    mode: str
    source: str
    elapsed_ms: float
    cache_read_ms: float
    homeserver_fetch_ms: float
    homeserver_scan_pages: int
    homeserver_scanned_event_count: int
    visible_event_count: int
    visible_event_ids: tuple[str, ...]
    cache_reject_reason: str | None
    degraded: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class ExpectationValidation:
    """Executable comparison of declared interaction expectations with observed evidence."""

    status: str
    interaction_records: int
    assertions: int
    strict_read_cache_isolated: bool


@dataclass(frozen=True, slots=True)
class AuditEvidence:
    """Sanitized durable evidence emitted by the live harness."""

    schema_version: int
    generated_at: str
    homeserver: str
    user_id: str
    room_id: str
    thread_root_id: str
    interactions: tuple[InteractionRecord, ...]
    media: tuple[dict[str, object], ...]
    request_timings: tuple[RequestTiming, ...]
    homeserver_event_ids: tuple[str, ...]
    homeserver_redaction_event_ids: tuple[str, ...]
    cache: CacheSnapshot | None
    accounting_missing_event_ids: tuple[str, ...]
    cache_only_event_ids: tuple[str, ...]
    trigger_event_ids: tuple[str, ...]
    thread_reads: tuple[ThreadReadRecord, ...]
    expectation_validation: ExpectationValidation | None
    notes: tuple[str, ...]


def new_transaction_id() -> str:
    """Return a fresh UUID transaction ID for one Matrix idempotent write."""
    transaction_id = str(uuid4())
    UUID(transaction_id)
    return transaction_id


def _tiny_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


def media_fixtures() -> tuple[MediaFixture, ...]:
    """Return deterministic real image, audio, video, and file fixtures."""
    return (
        MediaFixture("tiny.txt", "text/plain", b"Matrix cache audit fixture.\n"),
        MediaFixture("tiny.png", "image/png", base64.b64decode(_TINY_PNG_BASE64)),
        MediaFixture("silence.wav", "audio/wav", _tiny_wav()),
        MediaFixture("black.webm", "video/webm", base64.b64decode(_TINY_WEBM_BASE64)),
    )


def validate_media_fixtures(fixtures: tuple[MediaFixture, ...]) -> None:
    """Decode every generated media fixture before it may be uploaded."""
    by_name = {fixture.filename: fixture for fixture in fixtures}
    _validate_png(by_name["tiny.png"].payload)
    with wave.open(io.BytesIO(by_name["silence.wav"].payload), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getnframes() == 0:
            msg = "Generated WAV fixture is not non-empty mono audio"
            raise MatrixAuditError(msg)
    with tempfile.NamedTemporaryFile(suffix=".webm") as video:
        video.write(by_name["black.webm"].payload)
        video.flush()
        try:
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "csv=p=0",
                    video.name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            msg = "ffprobe is required; enter the documented Nix development shell"
            raise MatrixAuditError(msg) from exc
    if probe.returncode != 0 or not probe.stdout.strip():
        msg = "Generated WebM fixture has no decodable video stream"
        raise MatrixAuditError(msg)


def _validate_png(payload: bytes) -> None:  # noqa: C901
    """Validate PNG chunk checksums and compressed image data without optional packages."""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        msg = "Generated PNG fixture has an invalid signature"
        raise MatrixAuditError(msg)
    offset = 8
    dimensions: tuple[int, int] | None = None
    compressed_image = bytearray()
    saw_end = False
    while offset < len(payload):
        if offset + 12 > len(payload):
            msg = "Generated PNG fixture has a truncated chunk"
            raise MatrixAuditError(msg)
        chunk_length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(payload):
            msg = "Generated PNG fixture has a truncated payload"
            raise MatrixAuditError(msg)
        chunk_data = payload[offset + 8 : offset + 8 + chunk_length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + chunk_length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            msg = "Generated PNG fixture has an invalid chunk checksum"
            raise MatrixAuditError(msg)
        if chunk_type == b"IHDR":
            dimensions = struct.unpack(">II", chunk_data[:8])
        elif chunk_type == b"IDAT":
            compressed_image.extend(chunk_data)
        elif chunk_type == b"IEND":
            saw_end = True
            break
        offset = chunk_end
    if dimensions != (1, 1) or not compressed_image or not saw_end:
        msg = "Generated PNG fixture is not a complete 1x1 image"
        raise MatrixAuditError(msg)
    if not zlib.decompress(compressed_image):
        msg = "Generated PNG fixture has empty compressed image data"
        raise MatrixAuditError(msg)


def _encoded_path_segment(value: str) -> str:
    return quote(value, safe="")


class MatrixApi:
    """Minimal authenticated Matrix client API used by the disposable harness."""

    def __init__(
        self,
        *,
        base_url: str,
        access_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._access_token = access_token
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
            transport=transport,
        )
        self.timings: list[RequestTiming] = []

    async def __aenter__(self) -> MatrixApi:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        response = await self._client.request(
            method,
            path,
            content=content,
            headers=headers,
            json=json_body,
            params=params,
        )
        self.timings.append(
            RequestTiming(
                operation=operation,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                status_code=response.status_code,
            ),
        )
        if response.is_error:
            try:
                errcode = response.json().get("errcode")
            except (json.JSONDecodeError, AttributeError):
                errcode = None
            detail = f" ({errcode})" if isinstance(errcode, str) else ""
            msg = f"Matrix {operation} failed with HTTP {response.status_code}{detail}"
            raise MatrixAuditError(msg)
        try:
            parsed = response.json()
        except ValueError as exc:
            msg = f"Matrix {operation} returned malformed JSON"
            raise MatrixAuditError(msg) from exc
        if not isinstance(parsed, dict):
            msg = f"Matrix {operation} returned a non-object response"
            raise MatrixAuditError(msg)
        return parsed

    async def _request_bytes(
        self,
        method: str,
        path: str,
        *,
        operation: str,
    ) -> bytes:
        started = time.perf_counter()
        response = await self._client.request(method, path)
        self.timings.append(
            RequestTiming(
                operation=operation,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                status_code=response.status_code,
            ),
        )
        if response.is_error:
            msg = f"Matrix {operation} failed with HTTP {response.status_code}"
            raise MatrixAuditError(msg)
        return response.content

    async def whoami(self) -> tuple[str, str | None]:
        response = await self._request(
            "GET",
            "/_matrix/client/v3/account/whoami",
            operation="whoami",
        )
        user_id = response.get("user_id")
        device_id = response.get("device_id")
        if not isinstance(user_id, str):
            msg = "Matrix whoami response omitted user_id"
            raise MatrixAuditError(msg)
        return user_id, device_id if isinstance(device_id, str) else None

    async def create_private_room(self, *, invite_user_id: str | None) -> str:
        body: dict[str, object] = {
            "name": f"Matrix cache audit {uuid4()}",
            "preset": "private_chat",
            "visibility": "private",
        }
        if invite_user_id is not None:
            body["invite"] = [invite_user_id]
        response = await self._request(
            "POST",
            "/_matrix/client/v3/createRoom",
            operation="create_private_room",
            json_body=body,
        )
        room_id = response.get("room_id")
        if not isinstance(room_id, str):
            msg = "Matrix createRoom response omitted room_id"
            raise MatrixAuditError(msg)
        return room_id

    async def join(self, room_id: str) -> None:
        await self._request(
            "POST",
            f"/_matrix/client/v3/join/{_encoded_path_segment(room_id)}",
            operation="join_private_room",
            json_body={},
        )

    async def upload(self, fixture: MediaFixture) -> str:
        response = await self._request(
            "POST",
            "/_matrix/media/v3/upload",
            operation=f"upload:{fixture.filename}",
            content=fixture.payload,
            headers={"Content-Type": fixture.mime_type},
            params={"filename": fixture.filename},
        )
        content_uri = response.get("content_uri")
        if not isinstance(content_uri, str):
            msg = f"Matrix upload omitted content_uri for {fixture.filename}"
            raise MatrixAuditError(msg)
        return content_uri

    async def download(self, content_uri: str, *, filename: str) -> bytes:
        parsed = urlsplit(content_uri)
        if parsed.scheme != "mxc" or not parsed.netloc or not parsed.path.strip("/"):
            msg = f"Matrix upload returned an invalid MXC URI for {filename}"
            raise MatrixAuditError(msg)
        return await self._request_bytes(
            "GET",
            (
                f"/_matrix/client/v1/media/download/{_encoded_path_segment(parsed.netloc)}/"
                f"{_encoded_path_segment(parsed.path.strip('/'))}"
            ),
            operation=f"download:{filename}",
        )

    async def send_event(self, room_id: str, event_type: str, content: dict[str, object]) -> str:
        transaction_id = new_transaction_id()
        response = await self._request(
            "PUT",
            (
                f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/send/"
                f"{_encoded_path_segment(event_type)}/{transaction_id}"
            ),
            operation=f"send:{event_type}",
            json_body=content,
        )
        event_id = response.get("event_id")
        if not isinstance(event_id, str):
            msg = f"Matrix send omitted event_id for {event_type}"
            raise MatrixAuditError(msg)
        return event_id

    async def send_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, object],
        *,
        state_key: str = "",
    ) -> str:
        response = await self._request(
            "PUT",
            (
                f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/state/"
                f"{_encoded_path_segment(event_type)}/{_encoded_path_segment(state_key)}"
            ),
            operation=f"state:{event_type}",
            json_body=content,
        )
        event_id = response.get("event_id")
        if not isinstance(event_id, str):
            msg = f"Matrix state send omitted event_id for {event_type}"
            raise MatrixAuditError(msg)
        return event_id

    async def redact(self, room_id: str, event_id: str, *, reason: str) -> str:
        transaction_id = new_transaction_id()
        response = await self._request(
            "PUT",
            (
                f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/redact/"
                f"{_encoded_path_segment(event_id)}/{transaction_id}"
            ),
            operation="redact",
            json_body={"reason": reason},
        )
        redaction_event_id = response.get("event_id")
        if not isinstance(redaction_event_id, str):
            msg = "Matrix redaction response omitted event_id"
            raise MatrixAuditError(msg)
        return redaction_event_id

    async def typing(self, room_id: str, user_id: str) -> None:
        await self._request(
            "PUT",
            (f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/typing/{_encoded_path_segment(user_id)}"),
            operation="typing",
            json_body={"timeout": 1000, "typing": True},
        )

    async def receipt(self, room_id: str, event_id: str) -> None:
        await self._request(
            "POST",
            (
                f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/receipt/m.read/"
                f"{_encoded_path_segment(event_id)}"
            ),
            operation="receipt",
            json_body={},
        )

    async def presence(self, user_id: str) -> None:
        await self._request(
            "PUT",
            f"/_matrix/client/v3/presence/{_encoded_path_segment(user_id)}/status",
            operation="presence",
            json_body={"presence": "online", "status_msg": "cache audit"},
        )

    async def global_account_data(self, user_id: str) -> None:
        await self._request(
            "PUT",
            (f"/_matrix/client/v3/user/{_encoded_path_segment(user_id)}/account_data/com.mindroom.cache.audit"),
            operation="global_account_data",
            json_body={"audit": True},
        )

    async def room_account_data(self, user_id: str, room_id: str) -> None:
        await self._request(
            "PUT",
            (
                f"/_matrix/client/v3/user/{_encoded_path_segment(user_id)}/rooms/"
                f"{_encoded_path_segment(room_id)}/account_data/com.mindroom.cache.audit"
            ),
            operation="room_account_data",
            json_body={"audit": True},
        )

    async def to_device(self, user_id: str, device_id: str) -> None:
        transaction_id = new_transaction_id()
        await self._request(
            "PUT",
            f"/_matrix/client/v3/sendToDevice/com.mindroom.cache.audit/{transaction_id}",
            operation="to_device",
            json_body={"messages": {user_id: {device_id: {"audit": True}}}},
        )

    async def room_messages(self, room_id: str) -> tuple[dict[str, Any], ...]:
        """Read the complete disposable room timeline backwards."""
        events: list[dict[str, Any]] = []
        from_token: str | None = None
        while True:
            params: dict[str, str | int] = {"dir": "b", "limit": 100}
            if from_token is not None:
                params["from"] = from_token
            response = await self._request(
                "GET",
                f"/_matrix/client/v3/rooms/{_encoded_path_segment(room_id)}/messages",
                operation="room_messages",
                params=params,
            )
            chunk = response.get("chunk")
            if not isinstance(chunk, list):
                msg = "Matrix room messages response omitted chunk"
                raise MatrixAuditError(msg)
            events.extend(event for event in chunk if isinstance(event, dict))
            end = response.get("end")
            if not chunk or not isinstance(end, str) or end == from_token:
                break
            from_token = end
        return tuple(events)


def _record(
    records: list[InteractionRecord],
    *,
    family: str,
    event_type: str,
    event_id: str,
    visible: bool = False,
    threaded: bool = False,
    edit: bool = False,
    representation: str = "active",
    room_level: bool = True,
) -> str:
    records.append(
        InteractionRecord(
            family=family,
            event_type=event_type,
            event_id=event_id,
            expected_point_cache=representation == "active",
            expected_visible_thread_history=visible,
            expected_event_thread_mapping=threaded and representation == "active",
            expected_edit_index=edit and representation == "active",
            expected_representation=representation,
            expected_room_level=room_level,
        ),
    )
    return event_id


async def _emit_state_matrix(
    api: MatrixApi,
    room_id: str,
    user_id: str,
    records: list[InteractionRecord],
) -> None:
    state_cases: tuple[tuple[str, str, dict[str, object], str], ...] = (
        ("member", "m.room.member", {"membership": "join"}, user_id),
        ("name", "m.room.name", {"name": "Matrix cache audit"}, ""),
        ("topic", "m.room.topic", {"topic": "Disposable cache audit"}, ""),
        ("avatar", "m.room.avatar", {"url": "mxc://invalid.invalid/avatar"}, ""),
        (
            "power",
            "m.room.power_levels",
            {
                "events": {"org.matrix.msc3401.call.member": 0},
                "events_default": 0,
                "state_default": 50,
                "users": {user_id: 100},
            },
            "",
        ),
        ("join", "m.room.join_rules", {"join_rule": "invite"}, ""),
        ("history", "m.room.history_visibility", {"history_visibility": "shared"}, ""),
        ("guest", "m.room.guest_access", {"guest_access": "forbidden"}, ""),
        ("alias", "m.room.canonical_alias", {"alt_aliases": []}, ""),
        ("generic_state", "com.mindroom.cache.audit.state", {"audit": True}, "contract"),
        (
            "rtc_membership_focus",
            "org.matrix.msc3401.call.member",
            {
                "application": "m.call",
                "call_id": "",
                "device_id": "AUDIT",
                "foci_active": [{"focus_selection": "oldest_membership", "type": "livekit"}],
                "focus_active": {"focus_selection": "oldest_membership", "type": "livekit"},
                "scope": "m.room",
            },
            f"_{user_id}_AUDIT_m.call",
        ),
    )
    for family, event_type, content, state_key in state_cases:
        event_id = await api.send_state(room_id, event_type, content, state_key=state_key)
        _record(records, family=family, event_type=event_type, event_id=event_id)


async def _emit_message_matrix(
    api: MatrixApi,
    room_id: str,
    media_urls: dict[str, str],
    records: list[InteractionRecord],
) -> tuple[str, str]:
    root_id = await api.send_event(
        room_id,
        "m.room.message",
        {"body": "cache audit root", "msgtype": "m.text"},
    )
    _record(
        records,
        family="text_root",
        event_type="m.room.message",
        event_id=root_id,
        visible=True,
        threaded=True,
        room_level=False,
    )
    simple_messages: tuple[tuple[str, str, dict[str, object]], ...] = (
        ("notice", "m.notice", {"body": "notice"}),
        ("emote", "m.emote", {"body": "waves"}),
        ("location", "m.location", {"body": "location", "geo_uri": "geo:51.5,-0.1"}),
    )
    for family, msgtype, extra in simple_messages:
        event_id = await api.send_event(
            room_id,
            "m.room.message",
            {"msgtype": msgtype, **extra},
        )
        _record(records, family=family, event_type="m.room.message", event_id=event_id)

    thread_child_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": "thread child",
            "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
            "msgtype": "m.text",
        },
    )
    _record(
        records,
        family="explicit_thread",
        event_type="m.room.message",
        event_id=thread_child_id,
        visible=True,
        threaded=True,
        room_level=False,
    )
    reply_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": "relation-less reply",
            "m.relates_to": {"m.in_reply_to": {"event_id": thread_child_id}},
            "msgtype": "m.text",
        },
    )
    _record(
        records,
        family="relation_less_reply",
        event_type="m.room.message",
        event_id=reply_id,
        visible=True,
        threaded=True,
        room_level=False,
    )

    edits = (
        ("root_edit", root_id, "edited root", None),
        ("thread_child_edit", thread_child_id, "edited child", root_id),
        ("relation_less_reply_edit", reply_id, "edited reply", None),
    )
    for family, original_id, body, edit_thread_id in edits:
        new_content: dict[str, object] = {"body": body, "msgtype": "m.text"}
        if edit_thread_id is not None:
            new_content["m.relates_to"] = {"event_id": edit_thread_id, "rel_type": "m.thread"}
        event_id = await api.send_event(
            room_id,
            "m.room.message",
            {
                "body": f"* {body}",
                "m.new_content": new_content,
                "m.relates_to": {"event_id": original_id, "rel_type": "m.replace"},
                "msgtype": "m.text",
            },
        )
        _record(
            records,
            family=family,
            event_type="m.room.message",
            event_id=event_id,
            threaded=True,
            edit=True,
            room_level=False,
        )

    reference_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": "reference",
            "m.relates_to": {"event_id": thread_child_id, "rel_type": "m.reference"},
            "msgtype": "m.text",
        },
    )
    _record(
        records,
        family="reference",
        event_type="m.room.message",
        event_id=reference_id,
        visible=True,
        threaded=True,
        room_level=False,
    )

    reaction_id = await api.send_event(
        room_id,
        "m.reaction",
        {"m.relates_to": {"event_id": thread_child_id, "key": "👍", "rel_type": "m.annotation"}},
    )
    _record(
        records,
        family="reaction",
        event_type="m.reaction",
        event_id=reaction_id,
        representation="tombstone",
    )
    reaction_redaction_id = await api.redact(room_id, reaction_id, reason="reaction audit")
    _record(
        records,
        family="reaction_redaction",
        event_type="m.room.redaction",
        event_id=reaction_redaction_id,
        representation="omitted",
    )

    media_cases: tuple[tuple[str, str, dict[str, object]], ...] = (
        (
            "file",
            "m.file",
            {
                "body": "tiny.txt",
                "info": {"mimetype": "text/plain", "size": 28},
                "url": media_urls["tiny.txt"],
            },
        ),
        (
            "image",
            "m.image",
            {
                "body": "tiny.png",
                "info": {"h": 1, "mimetype": "image/png", "size": 68, "w": 1},
                "url": media_urls["tiny.png"],
            },
        ),
        (
            "audio_voice",
            "m.audio",
            {
                "body": "silence.wav",
                "info": {"duration": 20, "mimetype": "audio/wav"},
                "org.matrix.msc1767.audio": {"duration": 20, "waveform": [0]},
                "org.matrix.msc3245.voice": {},
                "url": media_urls["silence.wav"],
            },
        ),
        (
            "video",
            "m.video",
            {
                "body": "black.webm",
                "info": {"duration": 40, "h": 2, "mimetype": "video/webm", "w": 2},
                "url": media_urls["black.webm"],
            },
        ),
    )
    for family, msgtype, content in media_cases:
        event_id = await api.send_event(
            room_id,
            "m.room.message",
            {"msgtype": msgtype, **content},
        )
        _record(records, family=family, event_type="m.room.message", event_id=event_id)

    sticker_id = await api.send_event(
        room_id,
        "m.sticker",
        {
            "body": "tiny sticker",
            "info": {"h": 1, "mimetype": "image/png", "size": 68, "w": 1},
            "url": media_urls["tiny.png"],
        },
    )
    _record(records, family="sticker", event_type="m.sticker", event_id=sticker_id)
    return root_id, thread_child_id


async def _emit_poll_beacon_call_matrix(
    api: MatrixApi,
    room_id: str,
    user_id: str,
    records: list[InteractionRecord],
) -> None:
    poll_start_id = await api.send_event(
        room_id,
        "m.poll.start",
        {
            "m.poll.start": {
                "answers": [{"id": "a", "m.text": "A"}],
                "kind": "m.disclosed",
                "max_selections": 1,
                "question": {"m.text": "Pick"},
            },
        },
    )
    _record(records, family="poll_start", event_type="m.poll.start", event_id=poll_start_id)
    for family, event_type, payload_key in (
        ("poll_response", "m.poll.response", "m.poll.response"),
        ("poll_end", "m.poll.end", "m.poll.end"),
    ):
        event_id = await api.send_event(
            room_id,
            event_type,
            {
                payload_key: {"answers": ["a"]} if family == "poll_response" else {"m.text": "Closed"},
                "m.relates_to": {"event_id": poll_start_id, "rel_type": "m.reference"},
            },
        )
        _record(records, family=family, event_type=event_type, event_id=event_id)

    beacon_info_id = await api.send_state(
        room_id,
        "m.beacon_info",
        {"asset": {"type": "m.self"}, "description": "audit", "live": True, "timeout": 60000},
        state_key=user_id,
    )
    _record(records, family="beacon_info", event_type="m.beacon_info", event_id=beacon_info_id)
    beacon_id = await api.send_event(
        room_id,
        "m.beacon",
        {
            "m.relates_to": {"event_id": beacon_info_id, "rel_type": "m.reference"},
            "org.matrix.msc3488.location": {"description": "audit", "uri": "geo:51.5,-0.1"},
            "org.matrix.msc3488.ts": int(time.time() * 1000),
        },
    )
    _record(records, family="beacon", event_type="m.beacon", event_id=beacon_id)

    call_cases: tuple[tuple[str, str, dict[str, object]], ...] = (
        (
            "call_invite",
            "m.call.invite",
            {
                "call_id": "audit",
                "lifetime": 60000,
                "offer": {"sdp": "", "type": "offer"},
                "version": 1,
            },
        ),
        ("call_candidates", "m.call.candidates", {"call_id": "audit", "candidates": [], "version": 1}),
        (
            "call_answer",
            "m.call.answer",
            {"answer": {"sdp": "", "type": "answer"}, "call_id": "audit", "version": 1},
        ),
        (
            "call_select",
            "m.call.select_answer",
            {"call_id": "audit", "selected_party_id": "party", "version": 1},
        ),
        ("call_reject", "m.call.reject", {"call_id": "audit", "version": 1}),
        (
            "call_negotiate",
            "m.call.negotiate",
            {"call_id": "audit", "description": {"sdp": "", "type": "offer"}, "version": 1},
        ),
        ("call_hangup", "m.call.hangup", {"call_id": "audit", "version": 1}),
    )
    for family, event_type, content in call_cases:
        event_id = await api.send_event(room_id, event_type, content)
        _record(records, family=family, event_type=event_type, event_id=event_id)
    rtc_notification_id = await api.send_event(
        room_id,
        "org.matrix.msc4075.rtc.notification",
        {"notification_type": "ring"},
    )
    _record(
        records,
        family="rtc_notification",
        event_type="org.matrix.msc4075.rtc.notification",
        event_id=rtc_notification_id,
    )
    generic_id = await api.send_event(
        room_id,
        "com.mindroom.cache.audit",
        {"audit": True},
    )
    _record(records, family="generic_timeline", event_type="com.mindroom.cache.audit", event_id=generic_id)


async def _emit_redaction_matrix(
    api: MatrixApi,
    room_id: str,
    records: list[InteractionRecord],
    *,
    cache_db_path: Path,
) -> None:
    message_id = await api.send_event(
        room_id,
        "m.room.message",
        {"body": "redact this message", "msgtype": "m.text"},
    )
    _record(
        records,
        family="message_redaction_target",
        event_type="m.room.message",
        event_id=message_id,
        representation="tombstone",
    )
    message_redaction_id = await api.redact(room_id, message_id, reason="message audit")
    _record(
        records,
        family="message_redaction",
        event_type="m.room.redaction",
        event_id=message_redaction_id,
        representation="omitted",
    )

    original_id = await api.send_event(
        room_id,
        "m.room.message",
        {"body": "original with edit", "msgtype": "m.text"},
    )
    edit_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": "* dependent edit",
            "m.new_content": {"body": "dependent edit", "msgtype": "m.text"},
            "m.relates_to": {"event_id": original_id, "rel_type": "m.replace"},
            "msgtype": "m.text",
        },
    )
    _record(
        records,
        family="original_with_dependent_edit",
        event_type="m.room.message",
        event_id=original_id,
        representation="tombstone",
    )
    _record(
        records,
        family="dependent_edit",
        event_type="m.room.message",
        event_id=edit_id,
        edit=True,
        representation="tombstone",
    )
    await _wait_for_cache_edit_index(
        cache_db_path,
        room_id=room_id,
        edit_event_id=edit_id,
    )
    original_redaction_id = await api.redact(room_id, original_id, reason="original audit")
    _record(
        records,
        family="original_redaction",
        event_type="m.room.redaction",
        event_id=original_redaction_id,
        representation="omitted",
    )

    edit_only_original_id = await api.send_event(
        room_id,
        "m.room.message",
        {"body": "edit-only original", "msgtype": "m.text"},
    )
    edit_only_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": "* edit-only target",
            "m.new_content": {"body": "edit-only target", "msgtype": "m.text"},
            "m.relates_to": {"event_id": edit_only_original_id, "rel_type": "m.replace"},
            "msgtype": "m.text",
        },
    )
    _record(
        records,
        family="edit_only_original",
        event_type="m.room.message",
        event_id=edit_only_original_id,
    )
    _record(
        records,
        family="edit_only_target",
        event_type="m.room.message",
        event_id=edit_only_id,
        edit=True,
        representation="tombstone",
    )
    await _wait_for_cache_edit_index(
        cache_db_path,
        room_id=room_id,
        edit_event_id=edit_only_id,
    )
    edit_redaction_id = await api.redact(room_id, edit_only_id, reason="edit-only audit")
    _record(
        records,
        family="edit_only_redaction",
        event_type="m.room.redaction",
        event_id=edit_redaction_id,
        representation="omitted",
    )


async def _emit_trigger_sequence(
    api: MatrixApi,
    room_id: str,
    root_id: str,
    trigger_user_id: str,
    *,
    wait_seconds: float,
    records: list[InteractionRecord],
) -> tuple[str, ...]:
    trigger_ids: list[str] = []
    for index in range(1, 3):
        event_id = await api.send_event(
            room_id,
            "m.room.message",
            {
                "body": f"{trigger_user_id} cache audit trigger {index}",
                "m.mentions": {"user_ids": [trigger_user_id]},
                "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
                "msgtype": "m.text",
            },
        )
        trigger_ids.append(event_id)
        _record(
            records,
            family=f"thread_read_trigger_{index}",
            event_type="m.room.message",
            event_id=event_id,
            visible=True,
            threaded=True,
            room_level=False,
        )
        await asyncio.sleep(wait_seconds)

    redaction_id = await api.redact(room_id, trigger_ids[-1], reason="force cache rejection")
    _expect_tombstone(records, trigger_ids[-1])
    _record(
        records,
        family="thread_read_trigger_redaction",
        event_type="m.room.redaction",
        event_id=redaction_id,
        representation="omitted",
    )
    third_id = await api.send_event(
        room_id,
        "m.room.message",
        {
            "body": f"{trigger_user_id} cache audit trigger 3",
            "m.mentions": {"user_ids": [trigger_user_id]},
            "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
            "msgtype": "m.text",
        },
    )
    trigger_ids.append(third_id)
    _record(
        records,
        family="thread_read_trigger_3_after_redaction",
        event_type="m.room.message",
        event_id=third_id,
        visible=True,
        threaded=True,
        room_level=False,
    )
    await asyncio.sleep(wait_seconds)
    return tuple(trigger_ids)


async def _emit_encrypted_relation_matrix(
    api: MatrixApi,
    room_id: str,
    root_id: str,
    thread_child_id: str,
    records: list[InteractionRecord],
) -> None:
    encryption_id = await api.send_state(
        room_id,
        "m.room.encryption",
        {"algorithm": "m.megolm.v1.aes-sha2"},
    )
    _record(records, family="encryption_state", event_type="m.room.encryption", event_id=encryption_id)
    pin_id = await api.send_state(
        room_id,
        "m.room.pinned_events",
        {"pinned": [root_id]},
    )
    _record(records, family="pinned_events", event_type="m.room.pinned_events", event_id=pin_id)
    for family, relation in (
        ("opaque_encrypted_thread", {"event_id": root_id, "rel_type": "m.thread"}),
        ("opaque_encrypted_edit", {"event_id": thread_child_id, "rel_type": "m.replace"}),
        ("opaque_encrypted_reply", {"m.in_reply_to": {"event_id": thread_child_id}}),
        ("opaque_encrypted_reference", {"event_id": thread_child_id, "rel_type": "m.reference"}),
    ):
        event_id = await api.send_event(
            room_id,
            "m.room.encrypted",
            {
                "algorithm": "m.megolm.v1.aes-sha2",
                "ciphertext": "deliberately-undecryptable-audit-payload",
                "device_id": "AUDIT",
                "m.relates_to": relation,
                "sender_key": "audit-sender-key",
                "session_id": "audit-session",
            },
        )
        _record(
            records,
            family=family,
            event_type="m.room.encrypted",
            event_id=event_id,
            threaded=True,
            room_level=False,
        )


def _thread_read_record(
    sequence: int,
    *,
    elapsed_ms: float,
    history: ThreadHistoryResult,
) -> ThreadReadRecord:
    diagnostics = history.diagnostics
    source = str(diagnostics.get("thread_read_source", "unknown"))
    return ThreadReadRecord(
        sequence=sequence,
        mode="cache_hit" if source == "cache" else "full_scan",
        source=source,
        elapsed_ms=round(elapsed_ms, 3),
        cache_read_ms=float(diagnostics.get("cache_read_ms", 0.0)),
        homeserver_fetch_ms=float(diagnostics.get("homeserver_fetch_ms", 0.0)),
        homeserver_scan_pages=int(diagnostics.get("homeserver_scan_pages", 0)),
        homeserver_scanned_event_count=int(
            diagnostics.get("homeserver_scanned_event_count", 0),
        ),
        visible_event_count=len(history),
        visible_event_ids=tuple(message.event_id for message in history),
        cache_reject_reason=diagnostics.get("cache_reject_reason"),
        degraded=bool(diagnostics.get("thread_read_degraded", False)),
        error=diagnostics.get("thread_read_error"),
    )


def _expect_tombstone(records: list[InteractionRecord], event_id: str) -> None:
    for index, record in enumerate(records):
        if record.event_id == event_id:
            records[index] = replace(
                record,
                expected_point_cache=False,
                expected_event_thread_mapping=False,
                expected_edit_index=False,
                expected_representation="tombstone",
            )
            return
    msg = "Strict-read redaction target is absent from the interaction records"
    raise MatrixAuditError(msg)


async def _strict_thread_read_sequence(
    api: MatrixApi,
    *,
    config: AuditConfig,
    room_id: str,
    root_id: str,
    user_id: str,
    device_id: str | None,
    records: list[InteractionRecord],
) -> tuple[ThreadReadRecord, ...]:
    """Prove refill, cache hit, and rejection/refill in a disposable isolated cache."""
    if config.strict_read_cache_db_path is None:
        msg = "--strict-thread-reads requires --strict-read-cache-db"
        raise MatrixAuditError(msg)
    if config.strict_read_cache_db_path.exists():
        msg = "--strict-read-cache-db must name a new disposable database"
        raise MatrixAuditError(msg)
    cache = SqliteEventCache(config.strict_read_cache_db_path)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    client = nio.AsyncClient(
        config.base_url,
        user=user_id,
        device_id=device_id,
        ssl=ssl_context,
    )
    client.access_token = config.access_token
    reads: list[ThreadReadRecord] = []
    try:
        await cache.initialize()
        strict_child_id = await api.send_event(
            room_id,
            "m.room.message",
            {
                "body": "strict cache rejection child",
                "m.relates_to": {"event_id": root_id, "rel_type": "m.thread"},
                "msgtype": "m.text",
            },
        )
        _record(
            records,
            family="strict_read_rejection_target",
            event_type="m.room.message",
            event_id=strict_child_id,
            visible=True,
            threaded=True,
            room_level=False,
        )
        for sequence in (1, 2):
            started = time.perf_counter()
            history = await fetch_dispatch_thread_snapshot(
                client,
                room_id,
                root_id,
                cache,
                caller_label=f"matrix_cache_live_audit_{sequence}",
            )
            reads.append(
                _thread_read_record(
                    sequence,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    history=history,
                ),
            )
        if reads[0].source != "homeserver" or reads[1].source != "cache":
            msg = "Strict reads did not produce the required refill followed by cache hit"
            raise MatrixAuditError(msg)
        redaction_event_id = await api.redact(
            room_id,
            strict_child_id,
            reason="strict read rejection audit",
        )
        _expect_tombstone(records, strict_child_id)
        _record(
            records,
            family="strict_read_rejection_redaction",
            event_type="m.room.redaction",
            event_id=redaction_event_id,
            representation="omitted",
        )
        await cache.redact_event(room_id, strict_child_id)
        await cache.mark_thread_stale(
            room_id,
            root_id,
            reason="live_audit_redaction",
        )
        cache_state = await cache.get_thread_cache_state(room_id, root_id)
        if thread_cache_rejection_reason(cache_state) != "thread_invalidated_after_validation":
            msg = "Disposable cache redaction did not make the validated snapshot rejectable"
            raise MatrixAuditError(msg)
        started = time.perf_counter()
        history = await fetch_dispatch_thread_snapshot(
            client,
            room_id,
            root_id,
            cache,
            caller_label="matrix_cache_live_audit_3_after_redaction",
        )
        reads.append(
            _thread_read_record(
                3,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                history=history,
            ),
        )
        if reads[2].source != "homeserver" or reads[2].cache_reject_reason is None:
            msg = "Strict read after Matrix redaction did not reject and refill"
            raise MatrixAuditError(msg)
        return tuple(reads)
    finally:
        await client.close()
        await cache.close()


def _begin_readonly_snapshot(db: sqlite3.Connection) -> None:
    """Pin all audit queries to one query-only SQLite read transaction."""
    db.execute("PRAGMA query_only = ON")
    db.execute("BEGIN")


def _cache_contains_edit_index(
    cache_db_path: Path,
    *,
    room_id: str,
    edit_event_id: str,
) -> bool:
    """Return whether one edit reached the service cache through a read-only snapshot."""
    database_uri = f"file:{quote(str(cache_db_path.resolve()))}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as db:
        _begin_readonly_snapshot(db)
        return (
            db.execute(
                "SELECT 1 FROM event_edits WHERE room_id = ? AND edit_event_id = ?",
                (room_id, edit_event_id),
            ).fetchone()
            is not None
        )


async def _wait_for_cache_edit_index(
    cache_db_path: Path,
    *,
    room_id: str,
    edit_event_id: str,
    timeout_seconds: float = _CACHE_OBSERVATION_TIMEOUT_SECONDS,
) -> None:
    """Wait until the service observes an edit before emitting its dependent redaction."""
    deadline = time.monotonic() + timeout_seconds
    while not _cache_contains_edit_index(
        cache_db_path,
        room_id=room_id,
        edit_event_id=edit_event_id,
    ):
        if time.monotonic() >= deadline:
            msg = f"Service cache did not observe edit {edit_event_id} before redaction"
            raise MatrixAuditError(msg)
        await asyncio.sleep(0.25)


def read_cache_snapshot(cache_db_path: Path, room_id: str) -> CacheSnapshot:
    """Read cache evidence through one SQLite read-only/query-only snapshot."""
    database_uri = f"file:{quote(str(cache_db_path.resolve()))}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as db:
        _begin_readonly_snapshot(db)
        quick_check_row = db.execute("PRAGMA quick_check").fetchone()
        active_event_ids = tuple(
            row[0]
            for row in db.execute(
                "SELECT event_id FROM events WHERE room_id = ? ORDER BY event_id",
                (room_id,),
            ).fetchall()
        )
        tombstoned_event_ids = tuple(
            row[0]
            for row in db.execute(
                "SELECT event_id FROM redacted_events WHERE room_id = ? ORDER BY event_id",
                (room_id,),
            ).fetchall()
        )
        edit_event_ids = tuple(
            row[0]
            for row in db.execute(
                "SELECT edit_event_id FROM event_edits WHERE room_id = ? ORDER BY edit_event_id",
                (room_id,),
            ).fetchall()
        )
        event_thread_ids = tuple(
            row[0]
            for row in db.execute(
                "SELECT event_id FROM event_threads WHERE room_id = ? ORDER BY event_id",
                (room_id,),
            ).fetchall()
        )
        thread_state_rows = int(
            db.execute(
                "SELECT count(*) FROM thread_cache_state WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0],
        )
        orphan_edit_rows = int(
            db.execute(
                """
                SELECT count(*)
                FROM event_edits AS edit_index
                LEFT JOIN events AS event
                  ON event.event_id = edit_index.edit_event_id
                WHERE edit_index.room_id = ? AND event.event_id IS NULL
                """,
                (room_id,),
            ).fetchone()[0],
        )
        orphan_thread_rows = int(
            db.execute(
                """
                SELECT count(*)
                FROM event_threads AS thread_index
                LEFT JOIN events AS event
                  ON event.event_id = thread_index.event_id
                WHERE thread_index.room_id = ?
                  AND event.event_id IS NULL
                  AND thread_index.event_id != thread_index.thread_id
                """,
                (room_id,),
            ).fetchone()[0],
        )
    return CacheSnapshot(
        active_event_ids=active_event_ids,
        tombstoned_event_ids=tombstoned_event_ids,
        edit_event_ids=edit_event_ids,
        event_thread_ids=event_thread_ids,
        thread_state_rows=thread_state_rows,
        orphan_edit_rows=orphan_edit_rows,
        orphan_thread_rows=orphan_thread_rows,
        quick_check="" if quick_check_row is None else str(quick_check_row[0]),
    )


def _interaction_expectation_comparisons(
    record: InteractionRecord,
    *,
    homeserver_ids: set[str],
    redaction_ids: set[str],
    active_ids: set[str],
    tombstoned_ids: set[str],
    edit_ids: set[str],
    mapped_ids: set[str],
    initial_visible_ids: set[str] | None,
) -> list[tuple[str, object, object]]:
    """Return every executable comparison declared by one interaction record."""
    comparisons: list[tuple[str, object, object]] = [
        ("homeserver_event", record.event_id in homeserver_ids, True),
        ("point_cache", record.event_id in active_ids, record.expected_point_cache),
        (
            "tombstone",
            record.event_id in tombstoned_ids,
            record.expected_representation == "tombstone",
        ),
        (
            "event_thread_mapping",
            record.event_id in mapped_ids,
            record.expected_event_thread_mapping,
        ),
        ("edit_index", record.event_id in edit_ids, record.expected_edit_index),
    ]
    if initial_visible_ids is not None:
        comparisons.append(
            (
                "visible_thread_history",
                record.event_id in initial_visible_ids,
                record.expected_visible_thread_history,
            ),
        )
    if record.expected_representation == "omitted":
        comparisons.append(("redaction_envelope", record.event_id in redaction_ids, True))
    if record.expected_room_level:
        comparisons.extend(
            [
                ("room_level_mapping", record.event_id in mapped_ids, False),
                ("room_level_edit_index", record.event_id in edit_ids, False),
            ],
        )
    elif record.expected_representation == "active":
        comparisons.append(("thread_scoped_mapping", record.event_id in mapped_ids, True))
    return comparisons


def validate_interaction_expectations(
    records: tuple[InteractionRecord, ...],
    *,
    homeserver_event_ids: tuple[str, ...],
    homeserver_redaction_event_ids: tuple[str, ...],
    cache: CacheSnapshot,
    thread_reads: tuple[ThreadReadRecord, ...],
) -> ExpectationValidation:
    """Compare every declared cache expectation with secret-free observed state."""
    errors: list[str] = []
    assertions = 0
    homeserver_ids = set(homeserver_event_ids)
    redaction_ids = set(homeserver_redaction_event_ids)
    active_ids = set(cache.active_event_ids)
    tombstoned_ids = set(cache.tombstoned_event_ids)
    edit_ids = set(cache.edit_event_ids)
    mapped_ids = set(cache.event_thread_ids)
    initial_visible_ids = set(thread_reads[0].visible_event_ids) if thread_reads else None

    for record in records:
        comparisons = _interaction_expectation_comparisons(
            record,
            homeserver_ids=homeserver_ids,
            redaction_ids=redaction_ids,
            active_ids=active_ids,
            tombstoned_ids=tombstoned_ids,
            edit_ids=edit_ids,
            mapped_ids=mapped_ids,
            initial_visible_ids=initial_visible_ids,
        )
        assertions += len(comparisons)
        errors.extend(
            f"{record.family}.{field}: expected {expected!r}, observed {actual!r}"
            for field, actual, expected in comparisons
            if actual != expected
        )
    if cache.quick_check != "ok":
        errors.append(f"cache.quick_check: expected 'ok', observed {cache.quick_check!r}")
    assertions += 1
    if cache.orphan_edit_rows != 0:
        errors.append(f"cache.orphan_edit_rows: expected 0, observed {cache.orphan_edit_rows}")
    assertions += 1
    if cache.orphan_thread_rows != 0:
        errors.append(f"cache.orphan_thread_rows: expected 0, observed {cache.orphan_thread_rows}")
    assertions += 1
    if errors:
        detail = "; ".join(errors[:20])
        if len(errors) > 20:
            detail = f"{detail}; plus {len(errors) - 20} more"
        msg = f"Interaction expectation validation failed: {detail}"
        raise MatrixAuditError(msg)
    return ExpectationValidation(
        status="passed",
        interaction_records=len(records),
        assertions=assertions,
        strict_read_cache_isolated=bool(thread_reads),
    )


def _secret_free_evidence(
    evidence: AuditEvidence,
    *,
    access_tokens: tuple[str, ...],
) -> dict[str, object]:
    payload = asdict(evidence)

    def inspect(value: object, *, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).lower()
                if any(fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS):
                    msg = f"Evidence contains forbidden secret key at {'.'.join((*path, str(key)))}"
                    raise MatrixAuditError(msg)
                inspect(child, path=(*path, str(key)))
            return
        if isinstance(value, list | tuple):
            for index, child in enumerate(value):
                inspect(child, path=(*path, str(index)))
            return
        for access_token in access_tokens:
            if access_token and access_token in str(value):
                msg = f"Evidence contains an access-token value at {'.'.join(path)}"
                raise MatrixAuditError(msg)

    inspect(payload)
    return payload


async def run_audit(  # noqa: PLR0915
    config: AuditConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AuditEvidence:
    """Run the disposable interaction matrix and return sanitized evidence."""
    if config.cache_db_path is None or not config.strict_thread_reads:
        msg = "Audit evidence requires a service cache and strict thread reads"
        raise MatrixAuditError(msg)
    fixtures = media_fixtures()
    validate_media_fixtures(fixtures)
    records: list[InteractionRecord] = []
    invite_timings: tuple[RequestTiming, ...] = ()
    thread_reads: tuple[ThreadReadRecord, ...] = ()
    async with MatrixApi(
        base_url=config.base_url,
        access_token=config.access_token,
        transport=transport,
    ) as api:
        user_id, device_id = await api.whoami()
        room_id = await api.create_private_room(invite_user_id=config.invite_user_id)
        if config.invite_access_token is not None:
            async with MatrixApi(
                base_url=config.base_url,
                access_token=config.invite_access_token,
                transport=transport,
            ) as invite_api:
                invite_user_id, _ = await invite_api.whoami()
                if invite_user_id != config.invite_user_id:
                    msg = "Invite access token does not belong to --invite-user-id"
                    raise MatrixAuditError(msg)
                await invite_api.join(room_id)
                invite_timings = tuple(invite_api.timings)
        media_urls = {fixture.filename: await api.upload(fixture) for fixture in fixtures}
        for fixture in fixtures:
            downloaded = await api.download(media_urls[fixture.filename], filename=fixture.filename)
            if hashlib.sha256(downloaded).hexdigest() != fixture.sha256:
                msg = f"Authenticated media download changed {fixture.filename}"
                raise MatrixAuditError(msg)
        await _emit_state_matrix(api, room_id, user_id, records)
        root_id, thread_child_id = await _emit_message_matrix(
            api,
            room_id,
            media_urls,
            records,
        )
        await _emit_poll_beacon_call_matrix(api, room_id, user_id, records)
        await _emit_redaction_matrix(
            api,
            room_id,
            records,
            cache_db_path=config.cache_db_path,
        )

        trigger_ids: tuple[str, ...] = ()
        if config.trigger_user_id is not None:
            trigger_ids = await _emit_trigger_sequence(
                api,
                room_id,
                root_id,
                config.trigger_user_id,
                wait_seconds=config.trigger_wait_seconds,
                records=records,
            )

        await api.typing(room_id, user_id)
        await api.receipt(room_id, root_id)
        await api.presence(user_id)
        await api.global_account_data(user_id)
        await api.room_account_data(user_id, room_id)
        if device_id is not None:
            await api.to_device(user_id, device_id)
        await _emit_encrypted_relation_matrix(
            api,
            room_id,
            root_id,
            thread_child_id,
            records,
        )
        await asyncio.sleep(config.settle_seconds)
        if config.strict_thread_reads:
            thread_reads = await _strict_thread_read_sequence(
                api,
                config=config,
                room_id=room_id,
                root_id=root_id,
                user_id=user_id,
                device_id=device_id,
                records=records,
            )
            await asyncio.sleep(config.settle_seconds)
        homeserver_events = await api.room_messages(room_id)
        timings = tuple(api.timings) + invite_timings

    homeserver_event_ids = tuple(
        sorted(event_id for event in homeserver_events if isinstance((event_id := event.get("event_id")), str)),
    )
    redaction_event_ids = tuple(
        sorted(
            event_id
            for event in homeserver_events
            if event.get("type") == "m.room.redaction" and isinstance((event_id := event.get("event_id")), str)
        ),
    )
    cache = None if config.cache_db_path is None else read_cache_snapshot(config.cache_db_path, room_id)
    accounting_missing: tuple[str, ...] = ()
    cache_only: tuple[str, ...] = ()
    if cache is not None:
        represented_event_ids = set(cache.active_event_ids) | set(cache.tombstoned_event_ids) | set(redaction_event_ids)
        accounting_missing = tuple(sorted(set(homeserver_event_ids) - represented_event_ids))
        cache_only = tuple(sorted(set(cache.active_event_ids) - set(homeserver_event_ids)))
    expectation_validation = (
        validate_interaction_expectations(
            tuple(records),
            homeserver_event_ids=homeserver_event_ids,
            homeserver_redaction_event_ids=redaction_event_ids,
            cache=cache,
            thread_reads=thread_reads,
        )
        if cache is not None and thread_reads
        else None
    )

    evidence = AuditEvidence(
        schema_version=1,
        generated_at=datetime.now(UTC).isoformat(),
        homeserver=config.base_url.rstrip("/"),
        user_id=user_id,
        room_id=room_id,
        thread_root_id=root_id,
        interactions=tuple(records),
        media=tuple(
            {
                "bytes": len(fixture.payload),
                "filename": fixture.filename,
                "mime_type": fixture.mime_type,
                "sha256": fixture.sha256,
                "verified_authenticated_download": True,
            }
            for fixture in fixtures
        ),
        request_timings=timings,
        homeserver_event_ids=homeserver_event_ids,
        homeserver_redaction_event_ids=redaction_event_ids,
        cache=cache,
        accounting_missing_event_ids=accounting_missing,
        cache_only_event_ids=cache_only,
        trigger_event_ids=trigger_ids,
        thread_reads=thread_reads,
        expectation_validation=expectation_validation,
        notes=(
            "Joined complete state, invite/leave timelines, and device-list changes are covered deterministically by unit tests.",
            "Opaque encrypted events are deliberately undecryptable and are sent only after plaintext trigger evidence.",
            "Strict refill, cache-hit, and rejection reads use a disposable cache separate from the service database.",
        ),
    )
    access_tokens = tuple(token for token in (config.access_token, config.invite_access_token) if token is not None)
    _secret_free_evidence(evidence, access_tokens=access_tokens)
    return evidence


def write_evidence(evidence: AuditEvidence, config: AuditConfig) -> None:
    """Write sanitized evidence after a second secret scan."""
    if evidence.expectation_validation is None or evidence.expectation_validation.status != "passed":
        msg = "Evidence output requires complete passing cache and strict-read expectation validation"
        raise MatrixAuditError(msg)
    access_tokens = tuple(token for token in (config.access_token, config.invite_access_token) if token is not None)
    payload = _secret_free_evidence(evidence, access_tokens=access_tokens)
    config.evidence_path.parent.mkdir(parents=True, exist_ok=True)
    config.evidence_path.write_text(
        f"{json.dumps(payload, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )


def _parse_args() -> AuditConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--homeserver",
        default=os.environ.get("MATRIX_HOMESERVER", "https://mindroom.chat"),
    )
    parser.add_argument(
        "--access-token-env",
        default="MATRIX_ACCESS_TOKEN",
        help="Environment variable containing the token; tokens are never accepted as CLI arguments.",
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--cache-db",
        type=Path,
        help="Existing service cache inspected only through SQLite read-only/query-only mode.",
    )
    parser.add_argument(
        "--strict-read-cache-db",
        type=Path,
        help="New disposable SQLite path used only by the isolated strict-read sequence.",
    )
    parser.add_argument("--invite-user-id")
    parser.add_argument(
        "--invite-access-token-env",
        help="Environment variable for the invited test agent token; tokens are never CLI arguments.",
    )
    parser.add_argument("--trigger-user-id")
    parser.add_argument("--strict-thread-reads", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=5.0)
    parser.add_argument("--trigger-wait-seconds", type=float, default=20.0)
    args = parser.parse_args()
    access_token = os.environ.get(args.access_token_env)
    if not access_token:
        parser.error(f"{args.access_token_env} must contain the Matrix access token")
    if args.trigger_user_id is not None and args.invite_user_id != args.trigger_user_id:
        parser.error("--trigger-user-id must equal --invite-user-id for the private audit room")
    invite_access_token = None if args.invite_access_token_env is None else os.environ.get(args.invite_access_token_env)
    if args.invite_access_token_env is not None and not invite_access_token:
        parser.error(f"{args.invite_access_token_env} must contain the invited agent token")
    if not args.strict_thread_reads or args.cache_db is None or args.strict_read_cache_db is None:
        parser.error("evidence output requires --strict-thread-reads, --cache-db, and --strict-read-cache-db")
    if args.strict_read_cache_db is not None:
        if args.strict_read_cache_db.exists():
            parser.error("--strict-read-cache-db must name a new disposable database")
        if args.cache_db is not None and args.strict_read_cache_db.resolve() == args.cache_db.resolve():
            parser.error("--strict-read-cache-db must be separate from the read-only service cache")
    return AuditConfig(
        base_url=args.homeserver,
        access_token=access_token,
        invite_access_token=invite_access_token,
        evidence_path=args.evidence,
        cache_db_path=args.cache_db,
        strict_read_cache_db_path=args.strict_read_cache_db,
        invite_user_id=args.invite_user_id,
        trigger_user_id=args.trigger_user_id,
        strict_thread_reads=args.strict_thread_reads,
        settle_seconds=args.settle_seconds,
        trigger_wait_seconds=args.trigger_wait_seconds,
    )


def main() -> None:
    """Run the command-line audit."""
    config = _parse_args()
    evidence = asyncio.run(run_audit(config))
    write_evidence(evidence, config)
    print(
        json.dumps(
            {
                "evidence": str(config.evidence_path),
                "interactions": len(evidence.interactions),
                "room_id": evidence.room_id,
                "thread_root_id": evidence.thread_root_id,
            },
            sort_keys=True,
        ),
    )


if __name__ == "__main__":
    main()
