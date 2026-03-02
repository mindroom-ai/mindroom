"""Tests for image message handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Image

from mindroom.matrix import image_handler


class TestExtractCaption:
    """Test MSC2530-based caption extraction."""

    def _make_event(self, body: str, filename: str | None = None) -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageImage)
        event.body = body
        content: dict = {"body": body}
        if filename is not None:
            content["filename"] = filename
        event.source = {"content": content}
        return event

    def test_caption_when_filename_differs_from_body(self) -> None:
        """When filename is present and differs from body, body is a caption."""
        event = self._make_event(body="What is in this chart?", filename="chart.png")
        assert image_handler.extract_caption(event) == "What is in this chart?"

    def test_no_caption_when_filename_matches_body(self) -> None:
        """When filename equals body, there is no caption."""
        event = self._make_event(body="photo.jpg", filename="photo.jpg")
        assert image_handler.extract_caption(event) == "[Attached image]"

    def test_no_caption_when_filename_absent(self) -> None:
        """When filename field is absent, body is the filename."""
        event = self._make_event(body="IMG_1234.jpg")
        assert image_handler.extract_caption(event) == "[Attached image]"

    def test_no_caption_when_body_empty(self) -> None:
        """When body is empty, return default prompt."""
        event = self._make_event(body="", filename="photo.jpg")
        assert image_handler.extract_caption(event) == "[Attached image]"

    def test_caption_ending_with_image_extension(self) -> None:
        """Captions that end with image extensions are preserved."""
        event = self._make_event(body="analyze report.png", filename="report.png")
        assert image_handler.extract_caption(event) == "analyze report.png"

    def test_no_filename_no_body(self) -> None:
        """Both body and filename absent/empty."""
        event = self._make_event(body="")
        assert image_handler.extract_caption(event) == "[Attached image]"


class TestDownloadImage:
    """Test image download and decryption."""

    @pytest.mark.asyncio
    async def test_download_unencrypted_image(self) -> None:
        """Test downloading an unencrypted image from Matrix."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.url = "mxc://example.org/abc123"
        event.source = {"content": {"info": {"mimetype": "image/png"}}}

        response = MagicMock()
        response.body = b"image_data"
        client.download.return_value = response

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.content == b"image_data"
        assert result.mime_type == "image/png"
        client.download.assert_called_once_with("mxc://example.org/abc123")

    @pytest.mark.asyncio
    async def test_download_encrypted_image(self) -> None:
        """Test downloading and decrypting an encrypted image."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.url = "mxc://example.org/encrypted123"
        event.mimetype = "image/jpeg"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
                "info": {"mimetype": "image/jpeg"},
            },
        }

        response = MagicMock()
        response.body = b"encrypted_image_data"
        client.download.return_value = response

        with patch("mindroom.matrix.image_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_image_data"

            result = await image_handler.download_image(client, event)
            assert isinstance(result, Image)
            assert result.content == b"decrypted_image_data"
            assert result.mime_type == "image/jpeg"
            mock_decrypt.assert_called_once_with(
                b"encrypted_image_data",
                "test_key",
                "test_hash",
                "test_iv",
            )

    @pytest.mark.asyncio
    async def test_download_returns_none_on_error(self) -> None:
        """Test that download returns None on DownloadError."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.url = "mxc://example.org/fail"

        error_response = MagicMock(spec=nio.DownloadError)
        client.download.return_value = error_response

        result = await image_handler.download_image(client, event)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_returns_none_on_exception(self) -> None:
        """Test that exceptions from client.download() return None."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.url = "mxc://example.org/timeout"

        client.download.side_effect = TimeoutError("connection timed out")

        result = await image_handler.download_image(client, event)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_encrypted_image_missing_key_material_returns_none(self) -> None:
        """Test encrypted payloads missing key material fail gracefully."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.url = "mxc://example.org/encrypted_missing_keys"
        event.source = {
            "content": {
                "file": {
                    "key": {},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }

        response = MagicMock()
        response.body = b"encrypted_image_data"
        client.download.return_value = response

        result = await image_handler.download_image(client, event)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_encrypted_image_decrypt_error_returns_none(self) -> None:
        """Test decryption failures are handled without raising."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.url = "mxc://example.org/encrypted_bad"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }

        response = MagicMock()
        response.body = b"encrypted_image_data"
        client.download.return_value = response

        with patch("mindroom.matrix.image_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.side_effect = ValueError("bad ciphertext")
            result = await image_handler.download_image(client, event)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_leaves_mimetype_unset_when_missing(self) -> None:
        """Test that missing unencrypted mimetype remains unset."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.url = "mxc://example.org/notype"
        event.source = {"content": {}}

        response = MagicMock()
        response.body = b"image_data"
        client.download.return_value = response

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.mime_type is None

    @pytest.mark.asyncio
    async def test_encrypted_image_uses_event_mimetype(self) -> None:
        """Test that encrypted images use event.mimetype (nio-parsed)."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.url = "mxc://example.org/enc_webp"
        event.mimetype = "image/webp"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }

        response = MagicMock()
        response.body = b"encrypted_data"
        client.download.return_value = response

        with patch("mindroom.matrix.image_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_data"
            result = await image_handler.download_image(client, event)

        assert isinstance(result, Image)
        assert result.mime_type == "image/webp"

    @pytest.mark.asyncio
    async def test_encrypted_image_leaves_mimetype_unset_when_none(self) -> None:
        """Test that encrypted images keep mimetype unset when absent."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.url = "mxc://example.org/enc_notype"
        event.mimetype = None
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }

        response = MagicMock()
        response.body = b"encrypted_data"
        client.download.return_value = response

        with patch("mindroom.matrix.image_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_data"
            result = await image_handler.download_image(client, event)

        assert isinstance(result, Image)
        assert result.mime_type is None
