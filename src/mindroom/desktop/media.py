"""Encrypted Matrix media transport for desktop screenshots and shell output."""

from __future__ import annotations

import asyncio

import nio
from nio import crypto

from mindroom.desktop.protocol import (
    MAX_SCREENSHOT_BYTES,
    MAX_SHELL_OUTPUT_BYTES,
    SHELL_OUTPUT_MIME_TYPE,
    EncryptedDesktopMedia,
)
from mindroom.matrix.media import upload_content_uri, upload_media_bytes

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
    encrypted_bytes, encryption = crypto.attachments.encrypt_attachment(payload)
    try:
        # nio uploads ignore the client request timeout, so a stalled homeserver would wait forever.
        async with asyncio.timeout(timeout_seconds):
            response = await upload_media_bytes(
                client,
                encrypted_bytes,
                content_type="application/octet-stream",
                filename=f"{filename}.enc",
            )
    except TimeoutError as exc:
        msg = f"Matrix media upload did not finish within {timeout_seconds:g} seconds."
        raise DesktopMediaError(msg) from exc
    mxc_uri = upload_content_uri(response)
    if mxc_uri is None:
        msg = f"Matrix media upload failed: {response}"
        raise DesktopMediaError(msg)

    key = encryption.get("key")
    hashes = encryption.get("hashes")
    if not isinstance(key, dict) or not isinstance(hashes, dict):
        msg = "Matrix attachment encryption returned malformed key metadata."
        raise DesktopMediaError(msg)
    key_value = key.get("k")
    iv = encryption.get("iv")
    sha256 = hashes.get("sha256")
    if not isinstance(key_value, str) or not key_value:
        msg = "Matrix attachment encryption returned incomplete key metadata."
        raise DesktopMediaError(msg)
    if not isinstance(iv, str) or not iv:
        msg = "Matrix attachment encryption returned incomplete key metadata."
        raise DesktopMediaError(msg)
    if not isinstance(sha256, str) or not sha256:
        msg = "Matrix attachment encryption returned incomplete key metadata."
        raise DesktopMediaError(msg)
    return EncryptedDesktopMedia(
        url=mxc_uri,
        key=key_value,
        iv=iv,
        sha256=sha256,
        mime_type=mime_type,
        size=len(payload),
    )


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
        payload = crypto.attachments.decrypt_attachment(
            response.body,
            media.key,
            media.sha256,
            media.iv,
        )
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
