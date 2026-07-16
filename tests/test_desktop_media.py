"""Tests for encrypted Matrix screenshot media."""

from __future__ import annotations

from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.media import (
    DesktopMediaError,
    download_encrypted_screenshot,
    upload_encrypted_screenshot,
)

JPEG = b"\xff\xd8\xffdesktop-image"


@pytest.mark.asyncio
async def test_screenshot_is_encrypted_before_upload_and_authenticated_after_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The homeserver media payload never contains the screenshot plaintext."""
    uploaded: list[bytes] = []

    async def upload(
        _client: nio.AsyncClient,
        content: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> nio.UploadResponse:
        assert content_type == "application/octet-stream"
        assert filename.endswith(".enc")
        uploaded.append(content)
        return nio.UploadResponse("mxc://example.org/screenshot")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    client = AsyncMock(spec=nio.AsyncClient)

    media = await upload_encrypted_screenshot(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
    )

    assert uploaded
    assert uploaded[0] != JPEG
    assert JPEG not in uploaded[0]
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_screenshot(client, media) == JPEG


@pytest.mark.asyncio
async def test_screenshot_ciphertext_tampering_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A modified media object is rejected before any image reaches the model."""
    uploaded: list[bytes] = []

    async def upload(
        _client: nio.AsyncClient,
        content: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> nio.UploadResponse:
        assert content_type == "application/octet-stream"
        assert filename.endswith(".enc")
        uploaded.append(content)
        return nio.UploadResponse("mxc://example.org/screenshot")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    client = AsyncMock(spec=nio.AsyncClient)
    media = await upload_encrypted_screenshot(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
    )
    tampered = bytes([uploaded[0][0] ^ 1, *uploaded[0][1:]])
    client.download.return_value = nio.DownloadResponse(tampered, "application/octet-stream", None)

    with pytest.raises(DesktopMediaError, match="authentication or decryption failed"):
        await download_encrypted_screenshot(client, media)
