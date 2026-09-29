"""Tests for image message handling."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Image
from agno.utils.models.claude import _format_image_for_message
from aiohttp import ClientConnectionError

import mindroom.matrix.media as media_module
from mindroom.matrix import image_handler
from mindroom.matrix.media import (
    _sniff_image_mime_type,
    download_media_bytes,
    extract_media_caption,
    resolve_image_mime_type,
    upload_content_uri,
    upload_media_bytes,
)
from tests.matrix_media_helpers import FakeMediaResponse, media_response, requested_mxc

if TYPE_CHECKING:
    from collections.abc import Iterator


class TestExtractCaption:
    """Test MSC2530-based caption extraction."""

    def _make_event(self, body: str, filename: str | None = None) -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.body = body
        content: dict = {"body": body}
        if filename is not None:
            content["filename"] = filename
        event.source = {"content": content}
        return event

    def test_caption_when_filename_differs_from_body(self) -> None:
        """When filename is present and differs from body, body is a caption."""
        event = self._make_event(body="What is in this chart?", filename="chart.png")
        assert extract_media_caption(event, default="[Attached image]") == "What is in this chart?"

    def test_no_caption_when_filename_matches_body(self) -> None:
        """When filename equals body, there is no caption."""
        event = self._make_event(body="photo.jpg", filename="photo.jpg")
        assert extract_media_caption(event, default="[Attached image]") == "[Attached image]"

    def test_no_caption_when_filename_absent(self) -> None:
        """When filename field is absent, body is the filename."""
        event = self._make_event(body="IMG_1234.jpg")
        assert extract_media_caption(event, default="[Attached image]") == "[Attached image]"

    def test_no_caption_when_body_empty(self) -> None:
        """When body is empty, return default prompt."""
        event = self._make_event(body="", filename="photo.jpg")
        assert extract_media_caption(event, default="[Attached image]") == "[Attached image]"

    def test_caption_ending_with_image_extension(self) -> None:
        """Captions that end with image extensions are preserved."""
        event = self._make_event(body="analyze report.png", filename="report.png")
        assert extract_media_caption(event, default="[Attached image]") == "analyze report.png"

    def test_no_filename_no_body(self) -> None:
        """Both body and filename absent/empty."""
        event = self._make_event(body="")
        assert extract_media_caption(event, default="[Attached image]") == "[Attached image]"


class TestUploadContentUri:
    """Test Matrix upload response normalization."""

    def test_direct_upload_response_returns_content_uri(self) -> None:
        """A direct nio upload response should expose its MXC URI."""
        response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/media"})

        assert upload_content_uri(response) == "mxc://server/media"

    def test_tuple_upload_response_returns_first_response_content_uri(self) -> None:
        """The tuple shape returned by nio upload should normalize through its first item."""
        response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/media"})

        assert upload_content_uri((response, {"unused": True})) == "mxc://server/media"

    def test_missing_content_uri_returns_none(self) -> None:
        """Upload responses without content_uri should be treated as failed uploads."""
        response = MagicMock(spec=nio.UploadResponse)
        response.content_uri = ""

        assert upload_content_uri(response) is None

    def test_non_upload_response_returns_none(self) -> None:
        """Upload errors and unrelated objects should not produce an MXC URI."""
        assert upload_content_uri(object()) is None


class TestUploadMediaBytes:
    """Test Matrix byte payload upload helper."""

    @pytest.mark.asyncio
    async def test_upload_media_bytes_uses_nio_data_provider(self) -> None:
        """The helper should preserve nio's callback-shaped upload contract."""
        client = AsyncMock(spec=nio.AsyncClient)
        response = nio.UploadResponse.from_dict({"content_uri": "mxc://server/media"})
        client.upload.return_value = (response, {})

        result = await upload_media_bytes(
            client,
            b"payload",
            content_type="text/plain",
            filename="message.txt",
        )

        assert result == (response, {})
        upload_call = client.upload.call_args
        assert upload_call.kwargs["content_type"] == "text/plain"
        assert upload_call.kwargs["filename"] == "message.txt"
        assert upload_call.kwargs["filesize"] == 7
        assert upload_call.kwargs["data_provider"](None, None).read() == b"payload"


class TestDownloadImage:
    """Test image download and decryption."""

    @pytest.mark.asyncio
    async def test_download_unencrypted_image(self) -> None:
        """Test downloading an unencrypted image from Matrix."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/abc123"
        event.source = {"content": {"info": {"mimetype": "image/png"}}}

        client.send.return_value = media_response(b"image_data")

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.content == b"image_data"
        assert result.mime_type == "image/png"
        client.send.assert_awaited_once()
        assert requested_mxc(client.send.await_args.args[1]) == "mxc://example.org/abc123"

    @pytest.mark.asyncio
    async def test_download_encrypted_image(self) -> None:
        """Test downloading and decrypting an encrypted image."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
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

        client.send.return_value = media_response(b"encrypted_image_data")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment") as mock_decrypt:
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
    async def test_download_prefers_detected_mime_when_metadata_mismatches(self) -> None:
        """Payload signature should win when Matrix metadata MIME is incorrect."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/mismatch"
        event.source = {"content": {"info": {"mimetype": "image/jpeg"}}}

        client.send.return_value = media_response(b"\x89PNG\r\n\x1a\nrest")

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.mime_type == "image/png"

    @pytest.mark.asyncio
    async def test_download_returns_none_on_error(self) -> None:
        """Test that download returns None on DownloadError."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/fail"

        client.send.return_value = media_response(None)

        result = await image_handler.download_image(client, event)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_does_not_retry_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An attempt that runs out of time fails closed at once, so a stalling server costs one deadline."""
        monkeypatch.setattr(media_module, "_MXC_CONNECTION_RETRY_WAIT_SECONDS", 0)
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/timeout"

        client.send.side_effect = TimeoutError("connection timed out")

        result = await image_handler.download_image(client, event)
        assert result is None
        assert client.send.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ["mxc://./media", "mxc://../media", "mxc://example.org/.", "mxc://example.org/.."])
    async def test_download_refuses_dot_segments(self, url: str) -> None:
        """A server name or media ID that is a dot segment would name another download path, so nothing is sent."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = url

        assert await download_media_bytes(client, event) is None
        client.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_download_recovers_after_a_lost_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A dropped connection is retried, as nio's request loop did."""
        monkeypatch.setattr(media_module, "_MXC_CONNECTION_RETRY_WAIT_SECONDS", 0)
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/flaky"
        event.source = {"content": {"info": {"mimetype": "image/png"}}}

        client.send.side_effect = [ClientConnectionError("reset"), media_response(b"\x89PNG\r\n\x1a\nrest")]

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.content == b"\x89PNG\r\n\x1a\nrest"

    @pytest.mark.asyncio
    async def test_download_media_bytes_returns_none_on_server_error(self) -> None:
        """A failed Matrix download fails closed and releases the connection."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/invalid"
        client.send.return_value = FakeMediaResponse(status=500)

        result = await download_media_bytes(client, event)

        assert result is None
        assert client.send.return_value.released

    @pytest.mark.asyncio
    async def test_download_media_bytes_stops_reading_at_the_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Media larger than the ingestion limit is abandoned as it streams, never buffered whole."""
        monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 10)
        served: list[bytes] = []

        def body() -> Iterator[bytes]:
            for _ in range(100):
                served.append(b"12345")
                yield b"12345"

        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/huge"
        client.send.return_value = FakeMediaResponse(chunks=body())

        result = await download_media_bytes(client, event)

        assert result is None
        assert len(served) == 3

    @pytest.mark.asyncio
    async def test_download_media_bytes_refuses_a_declared_oversized_length_without_reading(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Content-Length above the limit is refused before any body byte is read."""
        monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 5)

        def unread_body() -> Iterator[bytes]:
            pytest.fail("Read the body of media that declared an oversized length")
            yield b""

        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/declared-too-large"
        client.send.return_value = FakeMediaResponse(chunks=unread_body(), content_length=6)

        assert await download_media_bytes(client, event) is None

    @pytest.mark.asyncio
    async def test_download_media_bytes_rejects_unencrypted_payload_over_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unencrypted Matrix media bytes should be capped before handler/model use."""
        monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 5)
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/too-large"
        client.send.return_value = media_response(b"123456")

        result = await download_media_bytes(client, event)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_media_bytes_rejects_encrypted_payload_over_limit_before_decrypt(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Oversized encrypted Matrix media should be rejected before decrypting."""
        monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 5)
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/encrypted-too-large"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }
        client.send.return_value = media_response(b"123456")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment") as mock_decrypt:
            result = await download_media_bytes(client, event)

        assert result is None
        mock_decrypt.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_media_bytes_rejects_decrypted_payload_over_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Decrypted Matrix media bytes should be capped before persistence or model handoff."""
        monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 5)
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/decrypted-too-large"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }
        client.send.return_value = media_response(b"small")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment", return_value=b"123456"):
            result = await download_media_bytes(client, event)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_encrypted_image_missing_key_material_returns_none(self) -> None:
        """Test encrypted payloads missing key material fail gracefully."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
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

        client.send.return_value = media_response(b"encrypted_image_data")

        result = await image_handler.download_image(client, event)
        assert result is None

    @pytest.mark.asyncio
    async def test_download_encrypted_image_decrypt_error_returns_none(self) -> None:
        """Test decryption failures are handled without raising."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
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

        client.send.return_value = media_response(b"encrypted_image_data")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.side_effect = ValueError("bad ciphertext")
            result = await image_handler.download_image(client, event)

        assert result is None

    @pytest.mark.asyncio
    async def test_download_leaves_mimetype_unset_when_missing(self) -> None:
        """Test that missing unencrypted mimetype remains unset."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageImage)
        event.event_id = "$test_event"
        event.url = "mxc://example.org/notype"
        event.source = {"content": {}}

        client.send.return_value = media_response(b"image_data")

        result = await image_handler.download_image(client, event)
        assert isinstance(result, Image)
        assert result.mime_type is None

    @pytest.mark.asyncio
    async def test_encrypted_image_uses_event_mimetype(self) -> None:
        """Test that encrypted images use event.mimetype (nio-parsed)."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
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

        client.send.return_value = media_response(b"encrypted_data")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_data"
            result = await image_handler.download_image(client, event)

        assert isinstance(result, Image)
        assert result.mime_type == "image/webp"

    @pytest.mark.asyncio
    async def test_encrypted_image_leaves_mimetype_unset_when_none(self) -> None:
        """Test that encrypted images keep mimetype unset when absent."""
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedImage)
        event.event_id = "$test_event"
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

        client.send.return_value = media_response(b"encrypted_data")

        with patch("mindroom.matrix.media.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_data"
            result = await image_handler.download_image(client, event)

        assert isinstance(result, Image)
        assert result.mime_type is None

    def test_anthropic_image_formatter_handles_raw_png_bytes(self) -> None:
        """Anthropic image formatting should work for Matrix-downloaded image bytes."""
        formatted = _format_image_for_message(Image(content=b"\x89PNG\r\n\x1a\npayload"))

        assert formatted is not None
        assert formatted["type"] == "image"
        assert formatted["source"]["media_type"] == "image/png"


class TestSniffImageMimeType:
    """Test lightweight image signature detection."""

    def test_sniff_known_formats(self) -> None:
        """Known image signatures should map to expected MIME types."""
        assert _sniff_image_mime_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
        assert _sniff_image_mime_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
        assert _sniff_image_mime_type(b"GIF89arest") == "image/gif"
        assert _sniff_image_mime_type(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"

    def test_sniff_unknown_returns_none(self) -> None:
        """Unknown byte prefixes should not be misclassified as images."""
        assert _sniff_image_mime_type(b"not-an-image") is None


class TestResolveImageMimeType:
    """Test effective image MIME resolution semantics."""

    def test_detected_type_takes_precedence_on_mismatch(self) -> None:
        """Signature-detected MIME should win when declared metadata is wrong."""
        result = resolve_image_mime_type(b"\x89PNG\r\n\x1a\npayload", "image/jpeg")
        assert result.effective_mime_type == "image/png"
        assert result.declared_mime_type == "image/jpeg"
        assert result.detected_mime_type == "image/png"
        assert result.is_mismatch is True

    def test_declared_type_used_when_detection_unavailable(self) -> None:
        """Declared MIME should be used when bytes do not match known signatures."""
        result = resolve_image_mime_type(b"unknown", "image/webp")
        assert result.effective_mime_type == "image/webp"
        assert result.declared_mime_type == "image/webp"
        assert result.detected_mime_type is None
        assert result.is_mismatch is False
