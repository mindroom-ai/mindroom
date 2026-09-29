"""Encrypted Matrix media transport for desktop screenshots and shell output."""

from __future__ import annotations

import asyncio

import nio

from mindroom.desktop.protocol import (
    MAX_SCREENSHOT_BYTES,
    MAX_SHELL_OUTPUT_BYTES,
    SHELL_OUTPUT_MIME_TYPE,
    DesktopMediaKind,
    DesktopProtocolError,
    EncryptedDesktopMedia,
)
from mindroom.matrix.media import decrypt_media_bytes, prepare_media_upload, upload_content_uri, upload_media_bytes

_IMAGE_SIGNATURES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}


class DesktopMediaError(RuntimeError):
    """One desktop media upload, download, or decryption operation failed."""


async def upload_encrypted_media(
    client: nio.AsyncClient,
    payload: bytes,
    *,
    mime_type: str,
    filename: str,
    timeout_seconds: float,
) -> EncryptedDesktopMedia:
    """Encrypt a screenshot or shell output locally and upload only ciphertext to Matrix media."""
    _validate_payload(payload, mime_type=mime_type)
    prepared = prepare_media_upload(payload, filename=filename, mimetype=mime_type, encrypt=True)
    file_content = prepared.encrypted_file_content()
    try:
        # nio uploads ignore the client request timeout, so a stalled homeserver would wait forever.
        async with asyncio.timeout(timeout_seconds):
            response = await upload_media_bytes(
                client,
                prepared.data,
                content_type=prepared.content_type,
                filename=prepared.filename,
            )
    except TimeoutError as exc:
        msg = f"Matrix media upload did not finish within {timeout_seconds:g} seconds."
        raise DesktopMediaError(msg) from exc
    mxc_uri = upload_content_uri(response)
    if mxc_uri is None:
        msg = f"Matrix media upload failed: {response}"
        raise DesktopMediaError(msg)
    if file_content is not None:
        file_content["url"] = mxc_uri
    try:
        # Receivers parse this reference strictly, so the bridge never sends one they would refuse.
        return EncryptedDesktopMedia.from_content(file_content, kind=_media_kind(mime_type))
    except DesktopProtocolError as exc:
        msg = f"Matrix media upload returned an invalid media reference: {exc}"
        raise DesktopMediaError(msg) from exc


async def download_encrypted_media(
    client: nio.AsyncClient,
    media: EncryptedDesktopMedia,
    *,
    timeout_seconds: float,
) -> bytes:
    """Download, authenticate, and decrypt one desktop media object, checking its declared size and type."""
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.download(media.url)
    except TimeoutError as exc:
        msg = f"Matrix media download did not finish within {timeout_seconds:g} seconds."
        raise DesktopMediaError(msg) from exc
    if not isinstance(response, nio.DownloadResponse) or not isinstance(response.body, bytes):
        msg = f"Matrix media download failed: {response}"
        raise DesktopMediaError(msg)
    if len(response.body) > _max_bytes(media.mime_type):
        msg = "Encrypted Matrix media exceeds the desktop media limit."
        raise DesktopMediaError(msg)
    try:
        payload = decrypt_media_bytes(response.body, key=media.key, sha256=media.sha256, iv=media.iv)
    except Exception as exc:
        msg = "Matrix media authentication or decryption failed."
        raise DesktopMediaError(msg) from exc
    if len(payload) != media.size:
        msg = "Decrypted Matrix media size does not match authenticated metadata."
        raise DesktopMediaError(msg)
    _validate_payload(payload, mime_type=media.mime_type)
    return payload


async def download_encrypted_screenshot(
    client: nio.AsyncClient,
    media: EncryptedDesktopMedia,
    *,
    timeout_seconds: float,
) -> bytes:
    """Download one desktop screenshot, refusing media of any other type."""
    if media.mime_type not in _IMAGE_SIGNATURES:
        msg = "Only screenshots can be downloaded as desktop images."
        raise DesktopMediaError(msg)
    return await download_encrypted_media(client, media, timeout_seconds=timeout_seconds)


def _media_kind(mime_type: str) -> DesktopMediaKind:
    return "output_attachment" if mime_type == SHELL_OUTPUT_MIME_TYPE else "screenshot"


def _max_bytes(mime_type: str) -> int:
    return MAX_SHELL_OUTPUT_BYTES if mime_type == SHELL_OUTPUT_MIME_TYPE else MAX_SCREENSHOT_BYTES


def _validate_payload(payload: bytes, *, mime_type: str) -> None:
    if not payload or len(payload) > _max_bytes(mime_type):
        msg = f"Desktop media must contain between 1 and {_max_bytes(mime_type)} bytes."
        raise DesktopMediaError(msg)
    if mime_type == SHELL_OUTPUT_MIME_TYPE:
        try:
            payload.decode()
        except UnicodeDecodeError as exc:
            msg = "Shell output media must be UTF-8 text."
            raise DesktopMediaError(msg) from exc
        return
    signature = _IMAGE_SIGNATURES.get(mime_type)
    if signature is None or not payload.startswith(signature):
        msg = "Desktop media bytes do not match their declared PNG, JPEG, or text MIME type."
        raise DesktopMediaError(msg)


__all__ = [
    "DesktopMediaError",
    "download_encrypted_media",
    "download_encrypted_screenshot",
    "upload_encrypted_media",
]
