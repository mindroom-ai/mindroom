"""Tests for large message handling."""

import json
from unittest.mock import MagicMock, patch

import nio
import pytest
from nio.crypto import OutboundGroupSession

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    ATTACHMENT_IDS_KEY,
    CONFIG_CONFIRMATION_REACTION_KEY,
    DURABLE_FINAL_OUTCOME_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    SKIP_MENTIONS_KEY,
    SOURCE_KIND_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_WARMUP_SUFFIX_KEY,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.interactive import parse_and_format_interactive
from mindroom.matrix.large_messages import (
    _MATRIX_EVENT_HARD_LIMIT,
    _NORMAL_MESSAGE_LIMIT,
    _SIDECAR_UPLOAD_FALLBACK_INDICATOR,
    _calculate_delivery_event_size,
    _calculate_event_size,
    _create_preview,
    _is_edit_message,
    _oversized_nonterminal_streaming_edit_sent_at,
    prepare_large_message,
    should_send_oversized_nonterminal_streaming_edit,
)
from mindroom.matrix.media import parse_matrix_media_event_source
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.tool_system.events import _TOOL_TRACE_KEY

_SIDECAR_UPLOAD_FALLBACK_TEXT = _SIDECAR_UPLOAD_FALLBACK_INDICATOR.strip()


class _UploadClient:
    rooms: dict = {}  # noqa: RUF012
    olm = None
    device_id = "DEVICE"

    def __init__(self, upload_result: object | BaseException) -> None:
        self.upload_result = upload_result
        self.uploaded_data: bytes | None = None

    async def upload(self, **kwargs) -> tuple[object, None]:  # noqa: ANN003
        if isinstance(self.upload_result, BaseException):
            raise self.upload_result
        data_provider = kwargs.get("data_provider")
        if data_provider:
            data = data_provider(None, None)
            self.uploaded_data = data.read()
        return self.upload_result, None


def _large_text_content(prefix: str) -> dict[str, str]:
    return {"body": prefix + ("x" * 100000), "msgtype": "m.text"}


def _actual_encrypted_event_size(
    content: dict[str, object],
    *,
    room_id: str,
    device_id: str = "DEVICE",
) -> int:
    """Encrypt one event with Megolm and measure the resulting content envelope."""
    session = OutboundGroupSession()
    session.shared = True
    plaintext = nio.Api.to_json(
        {
            "content": content,
            "type": "m.room.message",
            "room_id": room_id,
        },
    )
    encrypted_content: dict[str, object] = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "sender_key": "s" * 43,
        "ciphertext": session.encrypt(plaintext),
        "session_id": "i" * 43,
        "device_id": device_id,
    }
    relation = content.get("m.relates_to")
    if isinstance(relation, dict):
        encrypted_content["m.relates_to"] = relation
    stream_status = content.get(STREAM_STATUS_KEY)
    if isinstance(stream_status, str):
        encrypted_content[STREAM_STATUS_KEY] = stream_status
    if content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is True:
        encrypted_content[VISIBLE_ROUTER_VOICE_ECHO_KEY] = True
    config_reaction_id = content.get(CONFIG_CONFIRMATION_REACTION_KEY)
    if isinstance(config_reaction_id, str):
        encrypted_content[CONFIG_CONFIRMATION_REACTION_KEY] = config_reaction_id
    return _calculate_event_size(encrypted_content)


def test_encrypted_event_size_estimate_bounds_real_megolm_output() -> None:
    """The delivery estimate must include Megolm expansion without consuming a live session."""
    content: dict[str, object] = {
        "body": "x" * 45_750,
        "msgtype": "m.text",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        STREAM_STATUS_KEY: "completed",
        VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        CONFIG_CONFIRMATION_REACTION_KEY: "$reaction",
    }

    estimated = _calculate_delivery_event_size(
        content,
        room_id="!room:server",
        room_encrypted=True,
        device_id="DEVICE",
    )
    actual = _actual_encrypted_event_size(content, room_id="!room:server")

    assert _MATRIX_EVENT_HARD_LIMIT - 256 <= actual <= _MATRIX_EVENT_HARD_LIMIT
    assert actual <= estimated <= actual + 16


@pytest.mark.asyncio
async def test_prepare_encrypted_normal_message_fits_after_megolm_encryption() -> None:
    """A plaintext-sized normal message must be sidecarred when ciphertext would overflow."""
    content = {"body": "x" * 50_000, "msgtype": "m.text"}

    prepared = await prepare_large_message(
        _UploadClient(nio.UploadResponse("mxc://server/encrypted-normal-sidecar")),
        "!room:server",
        content,
        room_encrypted=True,
    )

    assert prepared["msgtype"] == "m.file"
    assert prepared["file"]["url"] == "mxc://server/encrypted-normal-sidecar"
    assert _actual_encrypted_event_size(prepared, room_id="!room:server") <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.parametrize("is_edit", [False, True])
@pytest.mark.asyncio
async def test_oversized_interactive_values_do_not_escape_the_matrix_event_limit(is_edit: bool) -> None:
    """Unbounded local selection values must not be copied into the preview event."""
    raw = json.dumps(
        {
            "question": "Choose",
            "options": [{"emoji": "1️⃣", "label": "Huge", "value": "x" * 70_000}],
        },
    )
    parsed = parse_and_format_interactive(f"```interactive\n{raw}\n```", extract_mapping=True)
    assert parsed.interactive_metadata is None
    assert "React with an emoji" not in parsed.formatted_text
    content: dict[str, object] = {"msgtype": "m.text", "body": parsed.formatted_text}
    if is_edit:
        content = {
            "msgtype": "m.text",
            "body": f"* {parsed.formatted_text}",
            "m.new_content": content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        }

    prepared = await prepare_large_message(
        _UploadClient(nio.UploadResponse("mxc://server/sidecar")),
        "!room:server",
        content,
    )

    assert "io.mindroom.interactive" not in prepared
    assert _calculate_event_size(prepared) <= _MATRIX_EVENT_HARD_LIMIT


def _assert_text_sidecar_fallback(result: dict[str, object], expected_prefix: str) -> None:
    assert result["msgtype"] == "m.text"
    assert isinstance(result["body"], str)
    assert result["body"].startswith(expected_prefix)
    assert _SIDECAR_UPLOAD_FALLBACK_TEXT in result["body"]
    assert "m.file" not in result.values()
    assert "filename" not in result
    assert "info" not in result
    assert "url" not in result
    assert "file" not in result
    assert "io.mindroom.long_text" not in result
    assert _calculate_event_size(result) <= _NORMAL_MESSAGE_LIMIT


def test_calculate_event_size() -> None:
    """Test event size calculation."""
    # Small message
    content = {"body": "Hello", "msgtype": "m.text"}
    size = _calculate_event_size(content)
    assert size < 3000  # Small message + overhead

    # Large message
    large_text = "x" * 50000
    content = {"body": large_text, "msgtype": "m.text"}
    size = _calculate_event_size(content)
    assert size > 50000
    assert size < 55000  # Text + overhead


def test__is_edit_message() -> None:
    """Test edit message detection."""
    # Regular message
    regular = {"body": "Hello", "msgtype": "m.text"}
    assert not _is_edit_message(regular)

    # Edit with m.new_content
    edit1 = {
        "body": "* Hello",
        "m.new_content": {"body": "Hello", "msgtype": "m.text"},
        "msgtype": "m.text",
    }
    assert _is_edit_message(edit1)

    # Edit with m.relates_to replace
    edit2 = {
        "body": "* Hello",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$123"},
        "msgtype": "m.text",
    }
    assert _is_edit_message(edit2)


def test_oversized_nonterminal_streaming_edit_rate_limit_prunes_expired_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized streaming-edit rate state should not retain old streams forever."""
    _oversized_nonterminal_streaming_edit_sent_at.clear()
    body = "x" * 40000

    def oversized_edit_content(original_event_id: str) -> dict[str, object]:
        return {
            "body": f"* {body}",
            "m.new_content": {
                "body": body,
                "msgtype": "m.text",
                STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
            "msgtype": "m.text",
        }

    monotonic_values = iter([100.0, 106.0])
    monkeypatch.setattr("mindroom.matrix.large_messages.monotonic", lambda: next(monotonic_values))

    assert should_send_oversized_nonterminal_streaming_edit(
        room_id="!room:server",
        original_event_id="$old",
        edit_content=oversized_edit_content("$old"),
    )
    assert _oversized_nonterminal_streaming_edit_sent_at == {("!room:server", "$old"): 100.0}

    assert should_send_oversized_nonterminal_streaming_edit(
        room_id="!room:server",
        original_event_id="$new",
        edit_content=oversized_edit_content("$new"),
    )

    assert _oversized_nonterminal_streaming_edit_sent_at == {("!room:server", "$new"): 106.0}


def test__create_preview() -> None:
    """Test preview creation."""
    # Short text - no truncation
    short_text = "Hello world"
    preview = _create_preview(short_text, 1000)
    assert preview == short_text

    # Long text - should truncate
    long_text = "Hello world. " * 1000
    preview = _create_preview(long_text, 1000)
    assert len(preview.encode("utf-8")) <= 1000
    assert "[Message continues in attached file]" in preview

    # Budget too small for any preview text — should return indicator only
    tiny_preview = _create_preview("Hello world. " * 1000, 10)
    assert tiny_preview == "[Message continues in attached file]"

    zero_preview = _create_preview("Hello world", 0)
    assert zero_preview == "[Message continues in attached file]"

    # Test natural break points
    paragraph_text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph." * 100
    preview = _create_preview(paragraph_text, 500)
    assert len(preview.encode("utf-8")) <= 500
    # Should break at paragraph boundary
    assert preview.count("\n\n") >= 1 or "[Message continues in attached file]" in preview


@pytest.mark.asyncio
async def test_prepare_large_message_passthrough() -> None:
    """Test that small messages pass through unchanged."""

    # Mock client
    class MockClient:
        rooms: dict = {}  # noqa: RUF012

    client = MockClient()

    # Small message should pass through
    small_content = {"body": "Small message", "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", small_content)
    assert result == small_content

    # Message just under limit should pass through
    text = "x" * (_NORMAL_MESSAGE_LIMIT - 3000)
    content = {"body": text, "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", content)
    assert result == content


@pytest.mark.asyncio
async def test_prepare_large_message_truncation() -> None:
    """Test that large messages get truncated with MXC upload."""

    # Mock client with upload - nio returns tuple
    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            # Create a mock UploadResponse
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file123"})
            return response, None  # nio returns (response, encryption_dict)

    client = MockClient()

    # Large message should get processed
    large_text = "x" * 100000  # 100KB
    content = {"body": large_text, "msgtype": "m.text"}
    result = await prepare_large_message(client, "!room:server", content)

    # Should be an m.file message
    assert result["msgtype"] == "m.file"
    assert "filename" in result
    assert result["filename"] == "message-content.json"

    # Should have file info
    assert "info" in result or "file" in result
    if "info" in result:
        expected_size = len(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        assert result["info"]["mimetype"] == "application/json"
        assert result["info"]["size"] == expected_size

    # Should have URL
    assert "url" in result or "file" in result

    # Should have custom metadata
    assert "io.mindroom.long_text" in result
    assert result["io.mindroom.long_text"]["version"] == 2
    assert result["io.mindroom.long_text"]["encoding"] == "matrix_event_content_json"
    assert result["io.mindroom.long_text"]["is_complete_content"] is True

    # Body should be truncated preview
    assert len(result["body"]) < len(large_text)
    assert "[Message continues in attached file]" in result["body"]

    assert client.uploaded_data is not None
    assert json.loads(client.uploaded_data.decode("utf-8")) == content

    # Preview should fit in limit
    assert _calculate_event_size(result) <= _NORMAL_MESSAGE_LIMIT


@pytest.mark.asyncio
async def test_prepare_large_message_upload_failure_falls_back_to_text() -> None:
    """Failed JSON sidecar uploads should not create m.file events without media references."""
    client = _UploadClient(RuntimeError("upload failed"))
    content = _large_text_content("upload failure ")

    result = await prepare_large_message(client, "!room:server", content)

    _assert_text_sidecar_fallback(result, "upload failure ")


@pytest.mark.asyncio
async def test_prepare_large_message_missing_content_uri_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload responses without MXC URIs should not create malformed m.file previews."""
    mock_logger = MagicMock()
    monkeypatch.setattr("mindroom.matrix.large_messages.logger", mock_logger)
    client = _UploadClient(nio.UploadResponse(""))
    content = _large_text_content("missing uri ")

    result = await prepare_large_message(client, "!room:server", content)

    _assert_text_sidecar_fallback(result, "missing uri ")
    mock_logger.warning.assert_any_call(
        "large_message_sidecar_unavailable_using_text_fallback",
        room_id="!room:server",
        original_size_bytes=_calculate_event_size(content),
        is_edit=False,
        has_mxc_uri=False,
        has_file_info=False,
    )
    assert client.uploaded_data is not None
    assert json.loads(client.uploaded_data.decode("utf-8")) == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_info",
    [
        None,
        {},
        {"size": 123},
        {"mimetype": "application/json"},
    ],
)
async def test_prepare_large_message_missing_sidecar_file_metadata_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
    file_info: dict[str, object] | None,
) -> None:
    """Sidecar uploads without usable file metadata should use text instead of broken m.file."""

    async def missing_file_metadata(
        _client: nio.AsyncClient,
        _room_id: str,
        _full_content: dict[str, object],
        *,
        room_encrypted: bool | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        assert room_encrypted is False
        return "mxc://server/missing-metadata", file_info

    monkeypatch.setattr("mindroom.matrix.large_messages.upload_json_sidecar", missing_file_metadata)
    client = _UploadClient(nio.UploadResponse("mxc://server/unused"))
    content = _large_text_content("missing metadata ")

    result = await prepare_large_message(client, "!room:server", content)

    _assert_text_sidecar_fallback(result, "missing metadata ")


@pytest.mark.asyncio
async def test_prepare_large_message_encrypted_incomplete_file_metadata_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted sidecars need encrypted file metadata, not just size and mimetype."""

    async def incomplete_encrypted_file_metadata(
        _client: nio.AsyncClient,
        _room_id: str,
        _full_content: dict[str, object],
        *,
        room_encrypted: bool | None = None,
    ) -> tuple[str, dict[str, object]]:
        assert room_encrypted is True
        return "mxc://server/incomplete-encrypted-metadata", {
            "size": 123,
            "mimetype": "application/json",
        }

    room = MagicMock()
    room.encrypted = True
    client = _UploadClient(nio.UploadResponse("mxc://server/unused"))
    client.rooms = {"!room:server": room}
    monkeypatch.setattr(
        "mindroom.matrix.large_messages.upload_json_sidecar",
        incomplete_encrypted_file_metadata,
    )
    content = _large_text_content("encrypted missing metadata ")

    result = await prepare_large_message(client, "!room:server", content)

    _assert_text_sidecar_fallback(result, "encrypted missing metadata ")


@pytest.mark.asyncio
async def test_prepare_large_message_encrypted_valid_sidecar_keeps_file_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted sidecars with complete file metadata should keep the m.file preview."""
    mxc_uri = "mxc://server/encrypted-sidecar"
    file_info = {
        "url": mxc_uri,
        "key": {"kty": "oct", "k": "secret"},
        "iv": "iv-value",
        "hashes": {"sha256": "sha256-value"},
        "v": "v2",
        "size": 123,
        "mimetype": "application/json",
    }

    async def encrypted_file_metadata(
        _client: nio.AsyncClient,
        _room_id: str,
        _full_content: dict[str, object],
        *,
        room_encrypted: bool | None = None,
    ) -> tuple[str, dict[str, object]]:
        assert room_encrypted is True
        return mxc_uri, file_info

    room = MagicMock()
    room.encrypted = True
    client = _UploadClient(nio.UploadResponse("mxc://server/unused"))
    client.rooms = {"!room:server": room}
    monkeypatch.setattr("mindroom.matrix.large_messages.upload_json_sidecar", encrypted_file_metadata)
    content = _large_text_content("encrypted sidecar ")

    result = await prepare_large_message(client, "!room:server", content)

    assert result["msgtype"] == "m.file"
    assert result["file"] == file_info
    assert "url" not in result
    assert result["io.mindroom.long_text"]["version"] == 2


@pytest.mark.asyncio
async def test_prepare_streaming_edit_encrypted_incomplete_file_metadata_omits_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming edit previews should not advertise unusable encrypted sidecars."""

    async def incomplete_encrypted_file_metadata(
        _client: nio.AsyncClient,
        _room_id: str,
        _full_content: dict[str, object],
        *,
        room_encrypted: bool | None = None,
    ) -> tuple[str, dict[str, object]]:
        assert room_encrypted is True
        return "mxc://server/incomplete-streaming-sidecar", {
            "size": 123,
            "mimetype": "application/json",
        }

    room = MagicMock()
    room.encrypted = True
    client = _UploadClient(nio.UploadResponse("mxc://server/unused"))
    client.rooms = {"!room:server": room}
    monkeypatch.setattr(
        "mindroom.matrix.large_messages.upload_json_sidecar",
        incomplete_encrypted_file_metadata,
    )
    text = "streaming encrypted fallback " + ("z" * 60000)
    edit_content = {
        "body": "* " + text,
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    inner = result["m.new_content"]
    assert inner["msgtype"] == "m.text"
    assert "file" not in inner
    assert "url" not in inner
    assert "io.mindroom.long_text" not in inner
    assert "[Streaming preview truncated]" in inner["body"]
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_edit_message_upload_failure_falls_back_to_text() -> None:
    """Edit fallback should stay textual and fit inside the Matrix hard limit."""
    client = _UploadClient(RuntimeError("upload failed"))
    text = "edit fallback " + ("y" * 50000)
    relates_to = {"rel_type": "m.replace", "event_id": "$abc"}
    edit_content = {
        "body": "* " + text,
        "m.new_content": {"body": text, "msgtype": "m.text"},
        "m.relates_to": relates_to,
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["msgtype"] == "m.text"
    assert result["m.relates_to"] == relates_to
    assert isinstance(result["body"], str)
    assert result["body"].startswith("* edit fallback ")
    inner = result["m.new_content"]
    assert inner["msgtype"] == "m.text"
    assert inner["m.relates_to"] == relates_to
    assert inner["body"].startswith("edit fallback ")
    assert _SIDECAR_UPLOAD_FALLBACK_TEXT in inner["body"]
    assert "m.file" not in inner.values()
    assert "filename" not in inner
    assert "info" not in inner
    assert "url" not in inner
    assert "file" not in inner
    assert "io.mindroom.long_text" not in inner
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_nonterminal_streaming_edit_double_fallback_preserves_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last-resort inline edit must keep nonterminal notice semantics."""

    def fail_rich_preview(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        "mindroom.matrix.large_messages._build_nonterminal_streaming_edit_preview",
        fail_rich_preview,
    )
    client = _UploadClient(RuntimeError("upload failed"))
    text = "streaming fallback " + ("y" * 50000)
    relates_to = {"rel_type": "m.replace", "event_id": "$abc"}
    edit_content = {
        "body": "* " + text,
        "m.new_content": {
            "body": text,
            "msgtype": "m.notice",
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
        },
        "m.relates_to": relates_to,
        "msgtype": "m.notice",
        STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["msgtype"] == "m.notice"
    assert result[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert result["m.relates_to"] == relates_to
    inner = result["m.new_content"]
    assert inner["msgtype"] == "m.notice"
    assert inner[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert _SIDECAR_UPLOAD_FALLBACK_TEXT in inner["body"]
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_large_message_valid_sidecar_keeps_file_preview() -> None:
    """Successful JSON sidecar uploads should keep the existing m.file preview behavior."""
    client = _UploadClient(nio.UploadResponse("mxc://server/sidecar"))
    content = _large_text_content("successful sidecar ")

    result = await prepare_large_message(client, "!room:server", content)

    assert result["msgtype"] == "m.file"
    assert result["url"] == "mxc://server/sidecar"
    assert result["info"]["mimetype"] == "application/json"
    assert result["io.mindroom.long_text"]["version"] == 2
    assert client.uploaded_data is not None
    assert json.loads(client.uploaded_data.decode("utf-8")) == content


@pytest.mark.asyncio
async def test_prepare_edit_message() -> None:
    """Test that edit messages use lower size threshold."""

    # Mock client with upload - nio returns tuple
    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            # Create a mock UploadResponse
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file456"})
            return response, None  # nio returns (response, encryption_dict)

    client = MockClient()

    # Message that's under normal limit but over edit limit
    text = "y" * 30000  # 30KB
    edit_content = {
        "body": "* " + text,
        "m.new_content": {"body": text, "msgtype": "m.text"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    # Should be processed due to edit limit
    # For edits, the structure is different - check for m.new_content
    assert "m.new_content" in result
    assert result["m.new_content"]["msgtype"] == "m.file"
    assert "io.mindroom.long_text" in result["m.new_content"]

    # Body should have preview
    assert len(result["body"]) < len("* " + text)
    # The rewrap rebuilds the fallback from the truncated preview rather than
    # carrying the original body forward. Length alone would pass on a fallback
    # truncated to the wrong text; only the preview it marks is right, and a
    # fallback still holding the full 30 KB would push the event over the limit
    # the truncation exists to stay under.
    assert result["body"] == f"* {result['m.new_content']['body']}"
    assert "[Message continues in attached file]" in result["m.new_content"]["body"]
    assert result["m.new_content"]["io.mindroom.long_text"]["version"] == 2
    assert client.uploaded_data is not None
    assert json.loads(client.uploaded_data.decode("utf-8")) == edit_content


@pytest.mark.asyncio
async def test_prepare_terminal_edit_keeps_local_recovery_data_off_the_wire() -> None:
    """Local recovery facts belong in the outbox, not its event or sidecar."""
    client = _UploadClient(nio.UploadResponse("mxc://server/final-edit"))
    text = "final answer " + ("x" * 20_000)
    semantic_outcome = {"body": text, "interactive": None}
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            DURABLE_FINAL_OUTCOME_KEY: semantic_outcome,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        DURABLE_FINAL_OUTCOME_KEY: semantic_outcome,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT
    assert DURABLE_FINAL_OUTCOME_KEY not in result
    assert DURABLE_FINAL_OUTCOME_KEY not in result["m.new_content"]
    assert client.uploaded_data is not None
    uploaded = json.loads(client.uploaded_data.decode("utf-8"))
    assert DURABLE_FINAL_OUTCOME_KEY not in uploaded
    assert DURABLE_FINAL_OUTCOME_KEY not in uploaded["m.new_content"]


@pytest.mark.asyncio
async def test_prepare_terminal_edit_preserves_bounded_result_compatibility_marker() -> None:
    """A version marker remains visible to older readers without carrying the result."""
    client = _UploadClient(nio.UploadResponse("mxc://server/final-edit-compatibility"))
    text = "final answer " + ("x" * 20_000)
    marker = {"version": 2}
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            DURABLE_FINAL_OUTCOME_KEY: marker,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        DURABLE_FINAL_OUTCOME_KEY: marker,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["m.new_content"][DURABLE_FINAL_OUTCOME_KEY] == {"version": 2}
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_terminal_edit_updates_sidecar_size_after_inner_preview_shrinks() -> None:
    """Fitting the replacement body must keep its sidecar metadata truthful."""
    client = _UploadClient(nio.UploadResponse("mxc://server/fitted-final-edit"))
    text = "final answer " + ("x" * 50_000)
    metadata = "m" * 20_000
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            "io.mindroom.required_metadata": metadata,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        "io.mindroom.required_metadata": metadata,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    inner = result["m.new_content"]
    assert len(inner["body"]) < 22_000
    assert inner["io.mindroom.long_text"]["preview_size"] == len(inner["body"])
    assert inner["io.mindroom.required_metadata"] == metadata
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_terminal_edit_uses_empty_previews_at_the_metadata_boundary() -> None:
    """Mandatory metadata may consume the final bytes without making the edit impossible."""
    client = _UploadClient(nio.UploadResponse("mxc://server/boundary-final-edit"))
    text = "x" * 61_400
    metadata = "m" * 30_706
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            "io.mindroom.required_metadata": metadata,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        "io.mindroom.required_metadata": metadata,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["body"] == ""
    assert result["m.new_content"]["body"] == ""
    assert result["m.new_content"]["io.mindroom.required_metadata"] == metadata
    assert _calculate_event_size(result) == _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_terminal_edit_fits_after_megolm_encryption() -> None:
    """Encrypted delivery must fit after nio replaces plaintext with ciphertext."""
    client = _UploadClient(nio.UploadResponse("mxc://server/encrypted-boundary-final-edit"))
    text = "x" * 61_400
    metadata = "m" * 20_000
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            "io.mindroom.required_metadata": metadata,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        "io.mindroom.required_metadata": metadata,
    }

    result = await prepare_large_message(
        client,
        "!room:server",
        edit_content,
        room_encrypted=True,
    )

    assert _actual_encrypted_event_size(result, room_id="!room:server") <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_terminal_edit_keeps_the_largest_outer_preview_that_fits() -> None:
    """A small overflow must trim only the bytes the finished envelope cannot carry."""
    client = _UploadClient(nio.UploadResponse("mxc://server/maximal-preview"))
    text = "x" * 20_500
    metadata = "m" * 10_500
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            "io.mindroom.test_metadata": metadata,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
        "io.mindroom.test_metadata": metadata,
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["m.new_content"]["body"] == text
    assert _calculate_event_size(result) <= _MATRIX_EVENT_HARD_LIMIT
    outer_preview = result["body"][2:]
    assert outer_preview.endswith("[Message continues in attached file]")
    visible_prefix_length = len(outer_preview) - len("\n\n[Message continues in attached file]")
    one_more_byte = dict(result)
    one_more_byte["body"] = f"* {text[: visible_prefix_length + 1]}\n\n[Message continues in attached file]"
    assert _calculate_event_size(one_more_byte) > _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_terminal_edit_rejects_irreducible_metadata_before_repeated_fitting() -> None:
    """An impossible envelope is detected from its empty-preview form."""
    client = _UploadClient(nio.UploadResponse("mxc://server/impossible-metadata"))
    text = "x" * 30_000
    edit_content = {
        "body": f"* {text}",
        "m.new_content": {
            "body": text,
            "msgtype": "m.text",
            "io.mindroom.required_metadata": "m" * 70_000,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        "msgtype": "m.text",
    }

    with (
        patch(
            "mindroom.matrix.large_messages._calculate_event_size",
            wraps=_calculate_event_size,
        ) as calculate_size,
        pytest.raises(ValueError, match="cannot fit within the Matrix event limit"),
    ):
        await prepare_large_message(client, "!room:server", edit_content)

    assert calculate_size.call_count <= 3


@pytest.mark.asyncio
async def test_sidecar_locator_overflow_falls_back_without_repreparing() -> None:
    """An unexpectedly large MXC URI must not turn preparation into a retry loop."""
    huge_mxc_uri = f"mxc://server/{'x' * 70_000}"
    encrypted_file = {
        "url": huge_mxc_uri,
        "key": {"kty": "oct", "k": "key"},
        "iv": "iv",
        "hashes": {"sha256": "hash"},
        "v": "v2",
        "mimetype": "application/json",
        "size": 70_000,
    }
    upload_count = 0

    async def upload_sidecar(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object]]:
        nonlocal upload_count
        upload_count += 1
        return huge_mxc_uri, encrypted_file

    content = {"msgtype": "m.text", "body": "x" * 70_000}
    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload_sidecar):
        result = await prepare_large_message(
            _UploadClient(nio.UploadResponse("mxc://server/unused")),
            "!room:server",
            content,
            room_encrypted=False,
        )

    assert upload_count == 1
    assert result["msgtype"] == "m.text"
    assert _SIDECAR_UPLOAD_FALLBACK_TEXT in result["body"]
    for sidecar_key in ("url", "file", "filename", "info", "io.mindroom.long_text"):
        assert sidecar_key not in result
    assert (
        _calculate_delivery_event_size(
            result,
            room_id="!room:server",
            room_encrypted=False,
            device_id=None,
        )
        <= _MATRIX_EVENT_HARD_LIMIT
    )


@pytest.mark.asyncio
async def test_prepare_oversized_file_edit_remains_parseable() -> None:
    """A sidecar-backed file edit must carry a valid outer fallback event."""
    client = _UploadClient(nio.UploadResponse("mxc://server/replacement-sidecar"))
    text = "updated file preview " * 3000
    edit_content = {
        "body": f"* {text}",
        "filename": "previous-message-content.json",
        "info": {"mimetype": "application/json", "size": 123},
        "m.new_content": {
            "body": text,
            "filename": "previous-message-content.json",
            "info": {"mimetype": "application/json", "size": 123},
            "msgtype": "m.file",
            "url": "mxc://server/previous-sidecar",
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        "msgtype": "m.file",
        "url": "mxc://server/previous-sidecar",
    }

    prepared = await prepare_large_message(client, "!room:server", edit_content)
    parsed = nio.Event.parse_event(
        {
            "content": prepared,
            "event_id": "$replacement",
            "sender": "@agent:server",
            "origin_server_ts": 123,
            "type": "m.room.message",
        },
    )

    assert isinstance(parsed, nio.RoomMessageFile)
    assert prepared["url"] == prepared["m.new_content"]["url"]
    assert prepared["filename"] == prepared["m.new_content"]["filename"]
    assert prepared["info"] == prepared["m.new_content"]["info"]
    assert _calculate_event_size(prepared) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_oversized_encrypted_file_edit_remains_parseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encrypted sidecar edits must carry their file descriptor on the fallback."""
    encrypted_file = {
        "url": "mxc://server/replacement-sidecar",
        "key": {
            "alg": "A256CTR",
            "ext": True,
            "key_ops": ["encrypt", "decrypt"],
            "kty": "oct",
            "k": "synthetic-key",
        },
        "iv": "synthetic-iv",
        "hashes": {"sha256": "synthetic-hash"},
        "v": "v2",
        "mimetype": "application/json",
        "size": 123,
    }

    async def upload_sidecar(*_args: object, **_kwargs: object) -> tuple[str, dict[str, object]]:
        return "mxc://server/replacement-sidecar", encrypted_file

    monkeypatch.setattr("mindroom.matrix.large_messages.upload_json_sidecar", upload_sidecar)
    text = "updated encrypted file preview " * 3000
    edit_content = {
        "body": f"* {text}",
        "file": {**encrypted_file, "url": "mxc://server/previous-sidecar"},
        "filename": "previous-message-content.json",
        "info": {"mimetype": "application/json", "size": 123},
        "m.new_content": {
            "body": text,
            "file": {**encrypted_file, "url": "mxc://server/previous-sidecar"},
            "filename": "previous-message-content.json",
            "info": {"mimetype": "application/json", "size": 123},
            "msgtype": "m.file",
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        "msgtype": "m.file",
    }

    prepared = await prepare_large_message(
        _UploadClient(nio.UploadResponse("mxc://server/unused")),
        "!room:server",
        edit_content,
        room_encrypted=True,
    )
    parsed = parse_matrix_media_event_source(
        {
            "content": prepared,
            "event_id": "$replacement",
            "sender": "@agent:server",
            "origin_server_ts": 123,
            "type": "m.room.message",
        },
    )

    assert isinstance(parsed, nio.RoomEncryptedFile)
    assert prepared["file"] == prepared["m.new_content"]["file"]
    assert prepared["filename"] == prepared["m.new_content"]["filename"]
    assert prepared["info"] == prepared["m.new_content"]["info"]
    assert _calculate_event_size(prepared) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.parametrize("room_encrypted", [False, True])
@pytest.mark.asyncio
async def test_prepare_oversized_file_edit_upload_failure_uses_parseable_text_fallback(
    room_encrypted: bool,
) -> None:
    """A failed replacement sidecar upload must not leave an invalid file wrapper."""
    client = _UploadClient(RuntimeError("upload failed"))
    text = "updated file fallback " * 3000
    edit_content = {
        "body": f"* {text}",
        "filename": "previous-message-content.json",
        "info": {"mimetype": "application/json", "size": 123},
        "m.new_content": {
            "body": text,
            "filename": "previous-message-content.json",
            "info": {"mimetype": "application/json", "size": 123},
            "msgtype": "m.file",
            "url": "mxc://server/previous-sidecar",
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        "msgtype": "m.file",
        "url": "mxc://server/previous-sidecar",
    }

    prepared = await prepare_large_message(
        client,
        "!room:server",
        edit_content,
        room_encrypted=room_encrypted,
    )

    for event_id, fallback in (("$replacement", prepared), ("$new-content", prepared["m.new_content"])):
        parsed = nio.Event.parse_event(
            {
                "content": fallback,
                "event_id": event_id,
                "sender": "@agent:server",
                "origin_server_ts": 123,
                "type": "m.room.message",
            },
        )
        assert isinstance(parsed, nio.RoomMessageText)
        assert fallback["msgtype"] == "m.text"
        for media_key in ("url", "file", "filename", "info"):
            assert media_key not in fallback

    assert _calculate_event_size(prepared) <= _MATRIX_EVENT_HARD_LIMIT


@pytest.mark.asyncio
async def test_prepare_nonterminal_streaming_edit_uses_rich_inline_preview() -> None:
    """Oversized in-progress stream edits keep an HTML preview and fresh sidecar."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/streaming-preview"})
            return response, None

    client = MockClient()
    text = ("streaming **markdown**\n\n🔧 `save_file` [1]\n" * 1000) + "tail"
    formatted_body = "<p>streaming <strong>markdown</strong></p><p>🔧 <code>save_file</code> [1]</p>" * 1000
    tool_trace = {"version": 2, "events": [{"type": "tool_call_started", "tool_name": "save_file"}]}
    edit_content = {
        "body": f"* {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
        "m.new_content": {
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_body,
            "msgtype": "m.text",
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            _TOOL_TRACE_KEY: tool_trace,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["m.new_content"]["msgtype"] == "m.text"
    assert result["m.new_content"][STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert result[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert result["m.new_content"]["format"] == "org.matrix.custom.html"
    assert "<strong>markdown</strong>" in result["m.new_content"]["formatted_body"]
    assert "Streaming preview truncated" in result["m.new_content"]["formatted_body"]
    assert _TOOL_TRACE_KEY not in result["m.new_content"]
    assert result["m.new_content"]["io.mindroom.long_text"]["version"] == 2
    assert result["m.new_content"]["io.mindroom.long_text"]["encoding"] == "matrix_event_content_json"
    assert result["m.new_content"]["url"] == "mxc://server/streaming-preview"
    assert "file" not in result["m.new_content"]
    assert len(result["m.new_content"]["body"]) < len(text)
    assert "[Streaming preview truncated]" in result["m.new_content"]["body"]
    assert "[Message continues in attached file]" not in result["m.new_content"]["body"]
    # The fallback layer is rebuilt from the same truncated preview. It is what
    # a client that ignores m.replace renders, and it is counted in the size
    # this whole path exists to keep under the hard limit -- so an untruncated
    # one is both the wrong text on screen and an event the server refuses.
    assert result["body"] == f"* {result['m.new_content']['body']}"
    assert result["formatted_body"] == result["m.new_content"]["formatted_body"]
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload == edit_content
    assert uploaded_payload["m.new_content"][_TOOL_TRACE_KEY] == tool_trace
    assert _calculate_event_size(result) <= 64000


@pytest.mark.asyncio
async def test_prepare_nonterminal_streaming_edit_keeps_preview_large_with_huge_sidecar_tool_trace() -> None:
    """Huge tool traces should go to the sidecar instead of shrinking visible preview."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/huge-trace"})
            return response, None

    client = MockClient()
    text = ("streaming **markdown**\n" * 2000) + "tail"
    huge_tool_trace = {
        "version": 2,
        "events": [
            {
                "type": "tool_call_completed",
                "tool_name": "save_file",
                "result_preview": "x" * 90000,
            },
        ],
    }
    edit_content = {
        "body": f"* {text}",
        "format": "org.matrix.custom.html",
        "formatted_body": "<p>streaming <strong>markdown</strong></p>" * 2000,
        "m.new_content": {
            "body": text,
            "format": "org.matrix.custom.html",
            "formatted_body": "<p>streaming <strong>markdown</strong></p>" * 2000,
            "msgtype": "m.text",
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            _TOOL_TRACE_KEY: huge_tool_trace,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert result["m.new_content"]["msgtype"] == "m.text"
    assert result["m.new_content"]["format"] == "org.matrix.custom.html"
    assert "<strong>markdown</strong>" in result["m.new_content"]["formatted_body"]
    assert "Streaming preview truncated" in result["m.new_content"]["formatted_body"]
    assert len(result["m.new_content"]["body"]) > 5000
    assert _TOOL_TRACE_KEY not in result["m.new_content"]
    assert result["m.new_content"]["io.mindroom.long_text"]["version"] == 2
    assert result["m.new_content"]["url"] == "mxc://server/huge-trace"
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload["m.new_content"][_TOOL_TRACE_KEY] == huge_tool_trace
    assert _calculate_event_size(result) <= 64000


@pytest.mark.asyncio
async def test_prepare_large_message_moves_tool_trace_to_json_sidecar_regular() -> None:
    """Large-message conversion keeps tool trace in uploaded sidecar, not preview."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file789"})
            return response, None

    client = MockClient()
    content = {
        "body": "z" * 100000,
        "msgtype": "m.text",
        _TOOL_TRACE_KEY: {"version": 1, "events": [{"type": "tool_call_started", "tool_name": "save_file"}]},
    }

    result = await prepare_large_message(client, "!room:server", content)
    assert _TOOL_TRACE_KEY not in result
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert _TOOL_TRACE_KEY in uploaded_payload


@pytest.mark.asyncio
async def test_prepare_large_message_preserves_ai_run_metadata() -> None:
    """AI run metadata should remain in the preview event for large messages."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file999"})
            return response, None

    client = MockClient()
    content = {
        "body": "m" * 100000,
        "msgtype": "m.text",
        AI_RUN_METADATA_KEY: {"version": 1, "usage": {"total_tokens": 1234}},
    }

    result = await prepare_large_message(client, "!room:server", content)
    assert AI_RUN_METADATA_KEY in result
    assert result[AI_RUN_METADATA_KEY]["usage"]["total_tokens"] == 1234
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload[AI_RUN_METADATA_KEY]["usage"]["total_tokens"] == 1234


@pytest.mark.asyncio
async def test_prepare_large_message_preserves_original_sender_metadata() -> None:
    """Original sender metadata should remain on large preview events for self-resume."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file1001"})
            return response, None

    client = MockClient()
    content = {
        "body": "n" * 100000,
        "msgtype": "m.text",
        ORIGINAL_SENDER_KEY: "@user:localhost",
    }

    result = await prepare_large_message(client, "!room:server", content)

    assert result[ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload[ORIGINAL_SENDER_KEY] == "@user:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_kind",
    ["scheduled", "hook", "hook_dispatch", "trusted_internal_relay"],
)
async def test_prepare_large_message_trusted_metadata_round_trips_through_sidecar_hydration(
    source_kind: str,
) -> None:
    """Large-message writer and reader should preserve trusted dispatch metadata."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None
        user_id = "@mindroom_agent:localhost"

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": f"mxc://server/{source_kind}-metadata-sidecar"})
            return response, None

        async def download(self, *, mxc: str) -> nio.DownloadResponse:
            assert mxc == f"mxc://server/{source_kind}-metadata-sidecar"
            assert self.uploaded_data is not None
            response = MagicMock(spec=nio.DownloadResponse)
            response.body = self.uploaded_data
            return response

    client = MockClient()
    content = {
        "body": "trusted metadata " * 10000,
        "msgtype": "m.text",
        SOURCE_KIND_KEY: source_kind,
        "com.mindroom.hook_source": "message_received",
        SKIP_MENTIONS_KEY: True,
        "formatted_body": '<a href="https://matrix.to/#/@mindroom_agent:localhost">agent</a>',
        "m.mentions": {"user_ids": ["@mindroom_agent:localhost"]},
        ATTACHMENT_IDS_KEY: ["att-sidecar"],
        HOOK_MESSAGE_RECEIVED_DEPTH_KEY: 2,
        ORIGINAL_SENDER_KEY: "@user:localhost",
        PER_FIRE_THREAD_ROOT_KEY: True,
        PER_FIRE_THREAD_ROOT_EVENT_ID_KEY: "$fire-root:localhost",
        VOICE_RAW_AUDIO_FALLBACK_KEY: True,
    }

    preview = await prepare_large_message(client, "!room:server", content)
    assert client.uploaded_data is not None
    assert preview[PER_FIRE_THREAD_ROOT_KEY] is True
    assert preview[PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] == "$fire-root:localhost"
    event = nio.RoomMessageText.from_dict(
        {
            "content": preview,
            "event_id": "$metadata-sidecar",
            "sender": "@mindroom_agent:localhost",
            "origin_server_ts": 123,
            "type": "m.room.message",
            "room_id": "!room:server",
        },
    )
    resolved = await extract_and_resolve_message(event, client)

    assert resolved["body"] == content["body"]
    assert resolved["content"][SOURCE_KIND_KEY] == source_kind
    assert resolved["content"]["com.mindroom.hook_source"] == "message_received"
    assert resolved["content"][SKIP_MENTIONS_KEY] is True
    assert resolved["content"]["formatted_body"] == content["formatted_body"]
    assert resolved["content"]["m.mentions"] == content["m.mentions"]
    assert resolved["content"][ATTACHMENT_IDS_KEY] == ["att-sidecar"]
    assert resolved["content"][HOOK_MESSAGE_RECEIVED_DEPTH_KEY] == 2
    assert resolved["content"][ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert resolved["content"][PER_FIRE_THREAD_ROOT_KEY] is True
    assert resolved["content"][PER_FIRE_THREAD_ROOT_EVENT_ID_KEY] == "$fire-root:localhost"
    assert resolved["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True


@pytest.mark.asyncio
async def test_prepare_large_message_moves_visible_body_to_json_sidecar_regular() -> None:
    """Large streamed previews should keep canonical visible body only in the JSON sidecar payload."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file1002"})
            return response, None

    client = MockClient()
    content = {
        "body": "v" * 100000,
        "msgtype": "m.text",
        "io.mindroom.visible_body": "v" * 100000,
    }

    result = await prepare_large_message(client, "!room:server", content)

    assert "io.mindroom.visible_body" not in result
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload["io.mindroom.visible_body"] == "v" * 100000


@pytest.mark.asyncio
async def test_prepare_large_message_keeps_explicit_warmup_suffix_on_preview() -> None:
    """Large streamed previews should retain the explicit warmup suffix metadata on the preview event."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file1002"})
            return response, None

    client = MockClient()
    warmup_suffix = "⏳ Preparing isolated worker..."
    content = {
        "body": ("v" * 100000) + f"\n\n{warmup_suffix}",
        "msgtype": "m.text",
        "io.mindroom.visible_body": "v" * 100000,
        STREAM_WARMUP_SUFFIX_KEY: warmup_suffix,
    }

    result = await prepare_large_message(client, "!room:server", content)

    assert result[STREAM_WARMUP_SUFFIX_KEY] == warmup_suffix
    assert "io.mindroom.visible_body" not in result


@pytest.mark.asyncio
async def test_prepare_large_message_moves_tool_trace_to_json_sidecar_edit() -> None:
    """Edit large-message conversion keeps tool trace in uploaded sidecar, not preview."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file999"})
            return response, None

    client = MockClient()
    edit_content = {
        "body": "* " + "w" * 50000,
        "m.new_content": {
            "body": "w" * 50000,
            "msgtype": "m.text",
            _TOOL_TRACE_KEY: {"version": 1, "events": [{"type": "tool_call_completed", "tool_name": "save_file"}]},
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)
    assert "m.new_content" in result
    assert _TOOL_TRACE_KEY not in result["m.new_content"]
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert _TOOL_TRACE_KEY in uploaded_payload["m.new_content"]


@pytest.mark.asyncio
async def test_prepare_large_message_moves_visible_body_to_json_sidecar_edit() -> None:
    """Large streamed edit previews should keep canonical visible body only in the JSON sidecar payload."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012
        uploaded_data: bytes | None = None

        async def upload(self, **kwargs) -> tuple:  # noqa: ANN003
            data_provider = kwargs.get("data_provider")
            if data_provider:
                data = data_provider(None, None)
                self.uploaded_data = data.read()
            response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/file1003"})
            return response, None

    client = MockClient()
    visible_body = "w" * 50000
    edit_content = {
        "body": "* " + visible_body,
        "m.new_content": {
            "body": visible_body,
            "msgtype": "m.text",
            "io.mindroom.visible_body": visible_body,
        },
        "io.mindroom.visible_body": visible_body,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(client, "!room:server", edit_content)

    assert "io.mindroom.visible_body" not in result
    assert "io.mindroom.visible_body" not in result["m.new_content"]
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload["m.new_content"]["io.mindroom.visible_body"] == visible_body
