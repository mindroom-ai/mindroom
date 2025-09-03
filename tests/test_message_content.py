"""Tests for centralized message content extraction with large message support."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.message_content import (
    clear_mxc_cache,
    download_mxc_text,
    extract_message_data,
    get_full_message_body,
    resolve_full_content,
)


class TestGetFullMessageBody:
    """Tests for get_full_message_body function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_regular_message_from_event(self) -> None:
        """Test extracting body from a regular RoomMessageText event."""
        event = MagicMock(spec=nio.RoomMessageText)
        event.body = "Hello world"
        event.source = {"content": {"msgtype": "m.text", "body": "Hello world"}}

        result = await get_full_message_body(event)
        assert result == "Hello world"

    @pytest.mark.asyncio
    async def test_regular_message_from_dict(self) -> None:
        """Test extracting body from a dict message."""
        message = {
            "body": "Test message",
            "content": {"msgtype": "m.text", "body": "Test message"},
        }

        result = await get_full_message_body(message)
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

        result = await get_full_message_body(message)
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

        result = await get_full_message_body(message, client)
        assert result == "Full message content that is very long"
        client.download.assert_called_once_with("server", "file123")

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
        result = await get_full_message_body(message, client)
        # Without proper crypto mocking, it will return preview
        assert result == "Preview..."


class TestDownloadMxcText:
    """Tests for download_mxc_text function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_invalid_mxc_url(self) -> None:
        """Test handling of invalid MXC URL."""
        client = AsyncMock()
        result = await download_mxc_text(client, "http://not-mxc-url")
        assert result is None

    @pytest.mark.asyncio
    async def test_malformed_mxc_url(self) -> None:
        """Test handling of malformed MXC URL."""
        client = AsyncMock()
        result = await download_mxc_text(client, "mxc://no-media-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_download(self) -> None:
        """Test successful text download."""
        client = AsyncMock()
        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"Downloaded text content"
        client.download.return_value = response

        result = await download_mxc_text(client, "mxc://server/media123")
        assert result == "Downloaded text content"

    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        """Test handling of download failure."""
        client = AsyncMock()
        client.download.return_value = MagicMock(spec=nio.DownloadError)

        result = await download_mxc_text(client, "mxc://server/media123")
        assert result is None


class TestExtractMessageData:
    """Tests for extract_message_data function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

    def test_extract_regular_message(self) -> None:
        """Test extracting data from regular message."""
        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:server"
        event.body = "Test message"
        event.server_timestamp = 1234567890
        event.event_id = "$event123"
        event.source = {"content": {"msgtype": "m.text", "body": "Test message"}}

        result = extract_message_data(event)

        assert result["sender"] == "@user:server"
        assert result["body"] == "Test message"
        assert result["timestamp"] == 1234567890
        assert result["event_id"] == "$event123"
        assert "_needs_full_content" not in result

    def test_extract_large_message_with_flag(self) -> None:
        """Test extracting data from large message with full content flag."""
        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:server"
        event.body = "Preview..."
        event.server_timestamp = 1234567890
        event.event_id = "$event123"
        event.source = {
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "io.mindroom.long_text": {"version": 1},
            },
        }

        result = extract_message_data(event, include_full_content=True)

        assert result["_needs_full_content"] is True
        assert result["_preview_body"] == "Preview..."

    def test_extract_large_message_without_flag(self) -> None:
        """Test extracting data from large message without full content flag."""
        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:server"
        event.body = "Preview..."
        event.server_timestamp = 1234567890
        event.event_id = "$event123"
        event.source = {
            "content": {
                "msgtype": "m.file",
                "body": "Preview...",
                "io.mindroom.long_text": {"version": 1},
            },
        }

        result = extract_message_data(event, include_full_content=False)

        assert "_needs_full_content" not in result
        assert result["body"] == "Preview..."


class TestResolveFullContent:
    """Tests for resolve_full_content function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

    @pytest.mark.asyncio
    async def test_resolve_regular_message(self) -> None:
        """Test that regular messages pass through unchanged."""
        client = AsyncMock()
        message = {
            "body": "Regular message",
            "content": {"msgtype": "m.text"},
        }

        result = await resolve_full_content(message, client)
        assert result == message

    @pytest.mark.asyncio
    async def test_resolve_marked_message(self) -> None:
        """Test resolving a message marked for full content."""
        client = AsyncMock()
        response = MagicMock(spec=nio.DownloadResponse)
        response.body = b"Full content here"
        client.download.return_value = response

        message = {
            "body": "Preview...",
            "_needs_full_content": True,
            "_preview_body": "Preview...",
            "content": {
                "msgtype": "m.file",
                "io.mindroom.long_text": {"version": 1},
                "url": "mxc://server/file123",
            },
        }

        result = await resolve_full_content(message, client)

        assert result["body"] == "Full content here"
        assert "_needs_full_content" not in result
        assert "_preview_body" not in result
