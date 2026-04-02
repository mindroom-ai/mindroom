"""Tests for centralized message content extraction with large message support."""

import json
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.message_content import (
    _clear_mxc_cache,
    _download_mxc_text,
    _get_full_message_body,
    extract_and_resolve_message,
    extract_edit_body,
)


def _make_message_event(
    *,
    body: str,
    content: dict[str, object],
    event_id: str = "$event",
    sender: str = "@alice:example.com",
    timestamp_ms: int = 1234567890,
) -> nio.RoomMessageText:
    """Create a Matrix text event for message content tests."""
    event = nio.RoomMessageText(
        source={
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": timestamp_ms,
            "type": "m.room.message",
        },
        body=body,
        formatted_body=None,
        format=None,
    )
    event.sender = sender
    return event


class TestGetFullMessageBody:
    """Tests for _get_full_message_body function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_regular_message(self) -> None:
        """Test extracting body from a regular message dict."""
        message = {
            "body": "Test message",
            "content": {"msgtype": "m.text", "body": "Test message"},
        }

        result = await _get_full_message_body(message)
        assert result == "Test message"

    @pytest.mark.asyncio
    async def test_large_message_without_client(self) -> None:
        """Test that large message returns preview when no client provided."""
        message = {
            "body": "Preview text...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview text...",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file123",
            },
        }

        result = await _get_full_message_body(message)
        assert result == "Preview text..."

    @pytest.mark.asyncio
    async def test_large_message_with_client_success(self) -> None:
        """Test successful download of large message content."""
        client = AsyncMock()
        client.download = AsyncMock()

        # Mock successful download
        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"Full message content that is very long"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file123",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Full message content that is very long"
        client.download.assert_called_once_with(mxc="mxc://server/file123")

    @pytest.mark.asyncio
    async def test_large_message_v2_json_sidecar_extracts_body(self) -> None:
        """V2 large-message sidecar JSON should resolve to the original body."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b'{"body":"Full v2 body","msgtype":"m.text","io.mindroom.tool_trace":{"version":2,"events":[]}}'
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/file-json",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Full v2 body"

    @pytest.mark.asyncio
    async def test_large_message_v2_invalid_json_returns_preview(self) -> None:
        """Invalid v2 payload JSON should fall back to preview body."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"not-json"
        client.download.return_value = response

        message = {
            "body": "Preview fallback",
            "content": {
                "msgtype": "m.file",
                "body": "Preview fallback",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/file-json-invalid",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Preview fallback"

    @pytest.mark.asyncio
    async def test_large_message_with_html_attachment_converts_to_text(self) -> None:
        """HTML attachments should resolve to plain text for prompt history."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"<h1>Title</h1><p>Hello <strong>world</strong></p><p>Second line</p>"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "info": {"mimetype": "text/html"},
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file-html",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Title\nHello world\nSecond line"

    @pytest.mark.asyncio
    async def test_large_message_with_html_link_preserves_url(self) -> None:
        """HTML links should preserve both label and URL for prompt history."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b'<p>See <a href="https://example.com/docs">the docs</a> now.</p>'
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "info": {"mimetype": "text/html"},
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file-html-link",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "See the docs (https://example.com/docs) now."

    @pytest.mark.asyncio
    async def test_large_message_with_html_single_quoted_link_preserves_url(self) -> None:
        """Single-quoted href links should also preserve URLs."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"<p>Docs: <a href='https://example.com/guide'>guide</a></p>"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "info": {"mimetype": "text/html"},
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file-html-single-quote-link",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Docs: guide (https://example.com/guide)"

    @pytest.mark.asyncio
    async def test_large_message_with_tool_html_attachment_converts_to_text(self) -> None:
        """HTML with tool markup should resolve to plain text for prompt history."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"<p>Before tool</p><tool><p>name: shell</p><p>cmd: ls -la</p></tool><p>After tool</p>"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "info": {"mimetype": "text/html"},
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file-tool-html",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Before tool\nname: shell\ncmd: ls -la\nAfter tool"

    @pytest.mark.asyncio
    async def test_large_message_with_html_filename_fallback_converts_to_text(self) -> None:
        """Missing mimetype falls back to filename extension."""
        client = AsyncMock()
        client.download = AsyncMock()

        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"<h2>Converted</h2><p>From filename fallback</p>"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "filename": "message.html",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "url": "mxc://server/file-filename-html",
            },
        }

        result = await _get_full_message_body(message, client)
        assert result == "Converted\nFrom filename fallback"

    @pytest.mark.asyncio
    async def test_large_message_with_encryption(self) -> None:
        """Test handling of encrypted large message."""
        client = AsyncMock()

        message = {
            "body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "io.mindroom.long_text": {
                    "version": 1,
                    "original_size": 100000,
                },
                "file": {
                    "url": "mxc://server/encrypted123",
                    "key": "encryption_key",
                    "hashes": {"sha256": "hash_value"},
                    "iv": "init_vector",
                },
            },
        }

        # For now, just verify it tries to get the URL from file info
        result = await _get_full_message_body(message, client)
        # Without proper crypto mocking, it will return preview
        assert result == "Preview..."


class TestResolvedMessageExtraction:
    """Tests for coherent visible message extraction."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_sidecar_content(self) -> None:
        """Regular v2 sidecars should return the canonical content and body."""
        original_content = {
            "msgtype": "m.text",
            "body": "Full response body",
            "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
        }
        event = _make_message_event(
            body="Preview body",
            content={
                "msgtype": "m.file",
                "body": "Preview body",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar",
            },
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(original_content).encode("utf-8"),
            ),
        )

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "Full response body"
        assert resolved["content"] == original_content

    @pytest.mark.asyncio
    async def test_extract_and_resolve_message_hydrates_v2_edit_wrapper(self) -> None:
        """Edit-sidecar events should resolve to the canonical outer replacement payload."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        event = _make_message_event(
            body="* Preview edit",
            content={
                "msgtype": "m.text",
                "body": "* Preview edit",
                "m.new_content": {
                    "msgtype": "m.file",
                    "body": "Preview edit",
                    "info": {"mimetype": "application/json"},
                    "io.mindroom.long_text": {
                        "version": 2,
                        "encoding": "matrix_event_content_json",
                    },
                    "url": "mxc://server/edit-sidecar",
                },
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        )
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        resolved = await extract_and_resolve_message(event, client)

        assert resolved["body"] == "* Full edit body"
        assert resolved["content"] == canonical_content
        assert resolved["content"]["body"] == resolved["body"]

    @pytest.mark.asyncio
    async def test_extract_edit_body_hydrates_v2_edit_sidecar(self) -> None:
        """Edit extraction should return the canonical m.new_content from a v2 sidecar."""
        canonical_content = {
            "msgtype": "m.text",
            "body": "* Full edit body",
            "m.new_content": {
                "msgtype": "m.text",
                "body": "Full edit body",
                "io.mindroom.tool_trace": {"version": 1, "events": [{"tool": "shell"}]},
            },
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        }
        client = AsyncMock(spec=nio.AsyncClient)
        client.download = AsyncMock(
            return_value=MagicMock(
                spec=nio.DownloadResponse,
                body=json.dumps(canonical_content).encode("utf-8"),
            ),
        )

        body, content = await extract_edit_body(
            {
                "content": {
                    "msgtype": "m.text",
                    "body": "* Preview edit",
                    "m.new_content": {
                        "msgtype": "m.file",
                        "body": "Preview edit",
                        "info": {"mimetype": "application/json"},
                        "io.mindroom.long_text": {
                            "version": 2,
                            "encoding": "matrix_event_content_json",
                        },
                        "url": "mxc://server/edit-sidecar",
                    },
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
                },
            },
            client,
        )

        assert body == "Full edit body"
        assert content == canonical_content["m.new_content"]


class TestDownloadMxcText:
    """Tests for _download_mxc_text function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        _clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_invalid_mxc_url(self) -> None:
        """Test handling of invalid MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "http://not-mxc-url")
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_mxc_url(self) -> None:
        """Test handling of malformed MXC URL."""
        client = AsyncMock()
        result = await _download_mxc_text(client, "mxc://no-media-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_download(self) -> None:
        """Test successful text download."""
        client = AsyncMock()
        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"Downloaded text content"
        client.download.return_value = response

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result == "Downloaded text content"
        client.download.assert_called_once_with(mxc="mxc://server/media123")

    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        """Test handling of download failure."""
        client = AsyncMock()
        client.download.return_value = MagicMock(spec=nio.DownloadError)

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result is None
