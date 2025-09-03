"""Tests for centralized message content extraction with large message support."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.matrix.message_content import (
    _download_mxc_text,
    _get_full_message_body,
    clear_mxc_cache,
)


class TestGetFullMessageBody:
    """Tests for _get_full_message_body function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

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
        result = await _get_full_message_body(message, client)
        # Without proper crypto mocking, it will return preview
        assert result == "Preview..."


class TestDownloadMxcText:
    """Tests for _download_mxc_text function."""

    def setup_method(self) -> None:
        """Clear cache before each test."""
        clear_mxc_cache()

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

    @pytest.mark.asyncio
    async def test_download_failure(self) -> None:
        """Test handling of download failure."""
        client = AsyncMock()
        client.download.return_value = MagicMock(spec=nio.DownloadError)

        result = await _download_mxc_text(client, "mxc://server/media123")
        assert result is None
