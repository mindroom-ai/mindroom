"""Tests for image message handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Image

from mindroom import image_handler


class TestIsFilename:
    """Test the filename detection heuristic."""

    @pytest.mark.parametrize(
        "body",
        [
            "IMG_1234.jpg",
            "photo.png",
            "screenshot.jpeg",
            "image.gif",
            "chart.webp",
            "diagram.svg",
            "photo.HEIC",
            "scan.tiff",
            "picture.avif",
        ],
    )
    def test_detects_image_filenames(self, body: str) -> None:
        """Test that known image filenames are correctly identified."""
        assert image_handler.is_filename(body) is True

    @pytest.mark.parametrize(
        "body",
        [
            "Analyze this chart.",
            "What do you see in this image?",
            "Here is a photo of my setup. Can you help?",
            "Hello",
            "",
            "no extension here",
            "report.pdf",
            "data.csv",
            "document.txt",
        ],
    )
    def test_rejects_captions_and_non_image_files(self, body: str) -> None:
        """Test that captions and non-image extensions are not filenames."""
        assert image_handler.is_filename(body) is False


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
        client.download.assert_called_once_with("mxc://example.org/abc123")

    @pytest.mark.asyncio
    async def test_download_encrypted_image(self) -> None:
        """Test downloading and decrypting an encrypted image."""
        client = AsyncMock()
        event = MagicMock(spec=["url", "source"])
        event.url = "mxc://example.org/encrypted123"
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

        with patch("mindroom.image_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_image_data"
            event.__class__ = nio.RoomEncryptedImage

            result = await image_handler.download_image(client, event)
            assert isinstance(result, Image)
            assert result.content == b"decrypted_image_data"
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
    async def test_download_defaults_mimetype_to_png(self) -> None:
        """Test that missing mimetype defaults to image/png."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.url = "mxc://example.org/notype"
        event.source = {"content": {}}

        response = MagicMock()
        response.body = b"image_data"
        client.download.return_value = response

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
