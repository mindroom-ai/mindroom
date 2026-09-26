"""Tests for encrypted Matrix screenshot media."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.desktop.media import (
    DesktopMediaError,
    download_encrypted_media,
    download_encrypted_screenshot,
    upload_encrypted_media,
)
from mindroom.desktop.protocol import MAX_SHELL_OUTPUT_BYTES, SHELL_OUTPUT_MIME_TYPE, EncryptedDesktopMedia

JPEG = b"\xff\xd8\xffdesktop-image"
OUTPUT = "shell output é \x01 😀\n".encode()


def _capture_uploads(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
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
        return nio.UploadResponse("mxc://example.org/media")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)
    return uploaded


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

    media = await upload_encrypted_media(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
    )

    assert uploaded
    assert uploaded[0] != JPEG
    assert JPEG not in uploaded[0]
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_screenshot(client, media, timeout_seconds=1) == JPEG


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
    media = await upload_encrypted_media(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
    )
    tampered = bytes([uploaded[0][0] ^ 1, *uploaded[0][1:]])
    client.download.return_value = nio.DownloadResponse(tampered, "application/octet-stream", None)

    with pytest.raises(DesktopMediaError, match="authentication or decryption failed"):
        await download_encrypted_screenshot(client, media, timeout_seconds=1)


@pytest.mark.asyncio
async def test_screenshot_download_timeout_is_bounded() -> None:
    """A stuck Matrix media request cannot hold the desktop tool open indefinitely."""
    client = AsyncMock(spec=nio.AsyncClient)

    async def stuck_download(_url: str) -> None:
        await asyncio.Event().wait()

    client.download.side_effect = stuck_download
    media = EncryptedDesktopMedia(
        url="mxc://example.org/screenshot",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/jpeg",
        size=len(JPEG),
    )

    with pytest.raises(DesktopMediaError, match="did not finish"):
        await download_encrypted_screenshot(client, media, timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_shell_output_round_trips_byte_exact_with_size_and_hash_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text output uses the screenshot encryption path and is authenticated the same way after download."""
    uploaded = _capture_uploads(monkeypatch)
    client = AsyncMock(spec=nio.AsyncClient)
    media = await upload_encrypted_media(client, OUTPUT, mime_type=SHELL_OUTPUT_MIME_TYPE, filename="shell.txt")
    assert (media.mime_type, media.size) == ("text/plain", len(OUTPUT))
    assert OUTPUT not in uploaded[0]
    assert EncryptedDesktopMedia.from_content(media.to_content(), kind="output_attachment") == media

    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    assert await download_encrypted_media(client, media, timeout_seconds=1) == OUTPUT
    with pytest.raises(DesktopMediaError, match="Only screenshots"):
        await download_encrypted_screenshot(client, media, timeout_seconds=1)

    tampered = bytes([uploaded[0][0] ^ 1, *uploaded[0][1:]])
    client.download.return_value = nio.DownloadResponse(tampered, "application/octet-stream", None)
    with pytest.raises(DesktopMediaError, match="authentication or decryption failed"):
        await download_encrypted_media(client, media, timeout_seconds=1)
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)
    with pytest.raises(DesktopMediaError, match="size does not match"):
        await download_encrypted_media(client, replace(media, size=media.size + 1), timeout_seconds=1)


@pytest.mark.parametrize(
    ("payload", "mime_type"),
    [
        (b"", SHELL_OUTPUT_MIME_TYPE),
        (b"\xff\xfe not utf-8", SHELL_OUTPUT_MIME_TYPE),
        (b"x" * (MAX_SHELL_OUTPUT_BYTES + 1), SHELL_OUTPUT_MIME_TYPE),
        (b"plain text", "image/png"),
        (JPEG, "application/octet-stream"),
    ],
)
@pytest.mark.asyncio
async def test_upload_rejects_payloads_that_do_not_match_their_type(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    mime_type: str,
) -> None:
    """Only bounded UTF-8 text or matching image bytes are encrypted and uploaded."""
    uploaded = _capture_uploads(monkeypatch)
    with pytest.raises(DesktopMediaError):
        await upload_encrypted_media(AsyncMock(spec=nio.AsyncClient), payload, mime_type=mime_type, filename="x")
    assert uploaded == []
