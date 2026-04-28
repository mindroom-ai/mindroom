"""Tests for large message handling."""

import json

import nio
import pytest

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.matrix.large_messages import (
    _NORMAL_MESSAGE_LIMIT,
    _calculate_event_size,
    _clear_oversized_nonterminal_streaming_sidecar_upload_rate_limits,
    _create_preview,
    _is_edit_message,
    _oversized_nonterminal_streaming_sidecar_uploaded_at,
    prepare_large_message,
    should_upload_oversized_nonterminal_stream_sidecar,
)
from mindroom.tool_system.events import _TOOL_TRACE_KEY


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


def test_oversized_nonterminal_streaming_sidecar_upload_rate_limit_prunes_expired_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized streaming-edit sidecar upload rate state should not retain old streams forever."""
    _clear_oversized_nonterminal_streaming_sidecar_upload_rate_limits()
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

    assert should_upload_oversized_nonterminal_stream_sidecar(
        room_id="!room:server",
        original_event_id="$old",
        edit_content=oversized_edit_content("$old"),
    )
    assert _oversized_nonterminal_streaming_sidecar_uploaded_at == {("!room:server", "$old"): 100.0}

    assert should_upload_oversized_nonterminal_stream_sidecar(
        room_id="!room:server",
        original_event_id="$new",
        edit_content=oversized_edit_content("$new"),
    )

    assert _oversized_nonterminal_streaming_sidecar_uploaded_at == {("!room:server", "$new"): 106.0}


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
    assert "[Message continues in attached file]" in result["m.new_content"]["body"]
    assert result["m.new_content"]["io.mindroom.long_text"]["version"] == 2
    assert client.uploaded_data is not None
    assert json.loads(client.uploaded_data.decode("utf-8")) == edit_content


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
    assert client.uploaded_data is not None
    uploaded_payload = json.loads(client.uploaded_data.decode("utf-8"))
    assert uploaded_payload == edit_content
    assert uploaded_payload["m.new_content"][_TOOL_TRACE_KEY] == tool_trace
    assert _calculate_event_size(result) <= 64000


@pytest.mark.asyncio
async def test_prepare_nonterminal_streaming_edit_can_skip_sidecar_upload() -> None:
    """Rate-limited in-progress stream edits should keep streaming previews without uploading."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012

        async def upload(self, **_kwargs: object) -> tuple:
            msg = "rate-limited non-terminal stream edit should not upload a sidecar"
            raise AssertionError(msg)

    text = ("streaming **markdown**\n" * 2000) + "tail"
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
            _TOOL_TRACE_KEY: {"version": 2, "events": [{"type": "tool_call_started", "tool_name": "save_file"}]},
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(
        MockClient(),
        "!room:server",
        edit_content,
        upload_nonterminal_stream_sidecar=False,
    )

    assert result["m.new_content"]["msgtype"] == "m.text"
    assert result["m.new_content"][STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert result["m.new_content"]["format"] == "org.matrix.custom.html"
    assert "Streaming preview truncated" in result["m.new_content"]["formatted_body"]
    assert _TOOL_TRACE_KEY not in result["m.new_content"]
    assert "io.mindroom.long_text" not in result["m.new_content"]
    assert "url" not in result["m.new_content"]
    assert "file" not in result["m.new_content"]
    assert _calculate_event_size(result) <= 64000


@pytest.mark.asyncio
async def test_prepare_nonterminal_streaming_edit_without_sidecar_drops_oversized_optional_metadata() -> None:
    """Preview-only stream edits must not upload when optional metadata is too large."""

    class MockClient:
        rooms: dict = {}  # noqa: RUF012

        async def upload(self, **_kwargs: object) -> tuple:
            msg = "preview-only non-terminal stream edit must not upload a sidecar"
            raise AssertionError(msg)

    text = ("streaming **markdown**\n" * 2000) + "tail"
    oversized_ai_run = {"version": 1, "debug": "x" * 90000}
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
            AI_RUN_METADATA_KEY: oversized_ai_run,
        },
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$abc"},
        "msgtype": "m.text",
    }

    result = await prepare_large_message(
        MockClient(),
        "!room:server",
        edit_content,
        upload_nonterminal_stream_sidecar=False,
    )

    assert result[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert result["m.new_content"][STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING
    assert AI_RUN_METADATA_KEY not in result
    assert AI_RUN_METADATA_KEY not in result["m.new_content"]
    assert "io.mindroom.long_text" not in result["m.new_content"]
    assert "url" not in result["m.new_content"]
    assert "file" not in result["m.new_content"]
    assert "Streaming preview truncated" in result["m.new_content"]["formatted_body"]
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
