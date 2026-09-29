"""Tests for encrypted Matrix screenshot media."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import nio
import pytest

from mindroom.desktop.media import (
    DesktopMediaError,
    download_encrypted_media,
    download_encrypted_screenshot,
    upload_encrypted_media,
)
from mindroom.desktop.protocol import (
    MAX_SCREENSHOT_BYTES,
    MAX_SHELL_OUTPUT_BYTES,
    SHELL_OUTPUT_MIME_TYPE,
    EncryptedDesktopMedia,
)

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
        timeout_seconds=1,
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
        timeout_seconds=1,
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
    media = await upload_encrypted_media(
        client,
        OUTPUT,
        mime_type=SHELL_OUTPUT_MIME_TYPE,
        filename="shell.txt",
        timeout_seconds=1,
    )
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
        await upload_encrypted_media(
            AsyncMock(spec=nio.AsyncClient),
            payload,
            mime_type=mime_type,
            filename="x",
            timeout_seconds=1,
        )
    assert uploaded == []


@pytest.mark.asyncio
async def test_upload_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A homeserver that never finishes an upload cannot hold the desktop bridge open indefinitely."""

    async def stuck_upload(*_args: object, **_kwargs: object) -> nio.UploadResponse:
        await asyncio.Event().wait()
        pytest.fail("stalled upload returned")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", stuck_upload)
    with pytest.raises(DesktopMediaError, match=r"upload did not finish within 0\.01 seconds"):
        await upload_encrypted_media(
            AsyncMock(spec=nio.AsyncClient),
            OUTPUT,
            mime_type=SHELL_OUTPUT_MIME_TYPE,
            filename="shell.txt",
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_upload_sends_only_ciphertext_and_returns_the_pinned_encrypted_file_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The homeserver sees an opaque `.enc` upload, and the returned reference keeps the Matrix encrypted-file wire shape."""
    calls: list[tuple[bytes, str, str]] = []

    async def upload(
        _client: nio.AsyncClient,
        content: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> tuple[nio.UploadResponse, None]:
        calls.append((content, content_type, filename))
        return nio.UploadResponse("mxc://example.org/screenshot"), None

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)

    media = await upload_encrypted_media(
        AsyncMock(spec=nio.AsyncClient),
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
        timeout_seconds=1,
    )

    [(ciphertext, content_type, filename)] = calls
    assert (content_type, filename) == ("application/octet-stream", "desktop.jpg.enc")
    assert len(ciphertext) == len(JPEG)
    assert ciphertext != JPEG
    content = media.to_content()
    assert content == {
        "url": "mxc://example.org/screenshot",
        "key": {
            "alg": "A256CTR",
            "ext": True,
            "k": media.key,
            "key_ops": ["encrypt", "decrypt"],
            "kty": "oct",
        },
        "iv": media.iv,
        "hashes": {"sha256": media.sha256},
        "v": "v2",
        "mimetype": "image/jpeg",
        "size": len(JPEG),
    }
    assert all((media.key, media.iv, media.sha256))
    assert EncryptedDesktopMedia.from_content(content) == media


@pytest.mark.asyncio
async def test_png_upload_encrypts_without_decoding_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """Desktop never sends Matrix image info, so a screenshot upload must not pay to decode its pixels."""
    uploaded = _capture_uploads(monkeypatch)

    def no_decode(*_args: object, **_kwargs: object) -> None:
        pytest.fail("desktop media upload decoded the image")

    monkeypatch.setattr("PIL.Image.open", no_decode)
    png = b"\x89PNG\r\n\x1a\ndesktop-image"

    media = await upload_encrypted_media(
        AsyncMock(spec=nio.AsyncClient),
        png,
        mime_type="image/png",
        filename="browser.png",
        timeout_seconds=1,
    )

    assert (media.mime_type, media.size) == ("image/png", len(png))
    assert png not in uploaded[0]


@pytest.mark.asyncio
async def test_upload_error_response_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """A homeserver upload error surfaces as a desktop media failure naming the response."""

    async def upload(*_args: object, **_kwargs: object) -> tuple[nio.UploadError, None]:
        return nio.UploadError("quota exceeded"), None

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)

    with pytest.raises(DesktopMediaError, match=r"^Matrix media upload failed: .*quota exceeded"):
        await upload_encrypted_media(
            AsyncMock(spec=nio.AsyncClient),
            OUTPUT,
            mime_type=SHELL_OUTPUT_MIME_TYPE,
            filename="shell.txt",
            timeout_seconds=1,
        )


@pytest.mark.parametrize(
    ("payload", "mime_type", "kind"),
    [(JPEG, "image/jpeg", "screenshot"), (OUTPUT, SHELL_OUTPUT_MIME_TYPE, "output_attachment")],
)
@pytest.mark.asyncio
async def test_upload_rejects_a_content_uri_that_receivers_would_refuse(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    mime_type: str,
    kind: str,
) -> None:
    """The bridge validates its upload with the receiver's parser, so a non-mxc reference never leaves it."""

    async def upload(*_args: object, **_kwargs: object) -> nio.UploadResponse:
        return nio.UploadResponse("https://example.org/media")

    monkeypatch.setattr("mindroom.desktop.media.upload_media_bytes", upload)

    with pytest.raises(
        DesktopMediaError,
        match=rf"^Matrix media upload returned an invalid media reference: {kind}\.url must be an mxc:// URI",
    ):
        await upload_encrypted_media(
            AsyncMock(spec=nio.AsyncClient),
            payload,
            mime_type=mime_type,
            filename="media",
            timeout_seconds=1,
        )


@pytest.mark.asyncio
async def test_download_error_response_is_reported() -> None:
    """A homeserver download error surfaces as a desktop media failure naming the response."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.download.return_value = nio.DownloadError("not found")
    media = EncryptedDesktopMedia(
        url="mxc://example.org/screenshot",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/jpeg",
        size=len(JPEG),
    )

    with pytest.raises(DesktopMediaError, match=r"^Matrix media download failed: .*not found"):
        await download_encrypted_media(client, media, timeout_seconds=1)


@pytest.mark.parametrize(
    ("mime_type", "limit"),
    [("image/jpeg", MAX_SCREENSHOT_BYTES), (SHELL_OUTPUT_MIME_TYPE, MAX_SHELL_OUTPUT_BYTES)],
)
@pytest.mark.asyncio
async def test_download_rejects_oversized_ciphertext_before_decrypting(
    monkeypatch: pytest.MonkeyPatch,
    mime_type: str,
    limit: int,
) -> None:
    """Ciphertext larger than the desktop media limit is refused without spending work on decryption."""
    decrypt = Mock()
    monkeypatch.setattr("nio.crypto.attachments.decrypt_attachment", decrypt)
    client = AsyncMock(spec=nio.AsyncClient)
    client.download.return_value = nio.DownloadResponse(b"x" * (limit + 1), "application/octet-stream", None)
    media = EncryptedDesktopMedia(
        url="mxc://example.org/media",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type=mime_type,
        size=limit,
    )

    with pytest.raises(DesktopMediaError, match=r"^Encrypted Matrix media exceeds the desktop media limit"):
        await download_encrypted_media(client, media, timeout_seconds=1)
    decrypt.assert_not_called()


@pytest.mark.asyncio
async def test_download_rejects_metadata_whose_sha256_does_not_match_the_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untampered ciphertext still fails closed when the authenticated SHA-256 names different bytes."""
    uploaded = _capture_uploads(monkeypatch)
    client = AsyncMock(spec=nio.AsyncClient)
    media = await upload_encrypted_media(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
        timeout_seconds=1,
    )
    other = await upload_encrypted_media(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
        timeout_seconds=1,
    )
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)

    with pytest.raises(DesktopMediaError, match=r"^Matrix media authentication or decryption failed"):
        await download_encrypted_screenshot(client, replace(media, sha256=other.sha256), timeout_seconds=1)


@pytest.mark.asyncio
async def test_download_rejects_plaintext_that_does_not_match_its_declared_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated bytes still have to match the declared image type before reaching a model."""
    uploaded = _capture_uploads(monkeypatch)
    client = AsyncMock(spec=nio.AsyncClient)
    media = await upload_encrypted_media(
        client,
        JPEG,
        mime_type="image/jpeg",
        filename="desktop.jpg",
        timeout_seconds=1,
    )
    client.download.return_value = nio.DownloadResponse(uploaded[0], "application/octet-stream", None)

    with pytest.raises(DesktopMediaError, match="do not match their declared PNG, JPEG, or text MIME type"):
        await download_encrypted_screenshot(client, replace(media, mime_type="image/png"), timeout_seconds=1)
