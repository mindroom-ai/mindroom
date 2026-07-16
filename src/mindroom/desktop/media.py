"""Encrypted Matrix media transport for desktop screenshots."""

from __future__ import annotations

import nio
from nio import crypto

from mindroom.desktop.protocol import MAX_SCREENSHOT_BYTES, EncryptedDesktopMedia
from mindroom.matrix.media import upload_content_uri, upload_media_bytes


class DesktopMediaError(RuntimeError):
    """One screenshot upload, download, or decryption operation failed."""


async def upload_encrypted_screenshot(
    client: nio.AsyncClient,
    image_bytes: bytes,
    *,
    mime_type: str,
    filename: str,
) -> EncryptedDesktopMedia:
    """Encrypt screenshot bytes locally and upload only ciphertext to Matrix media."""
    _validate_image_payload(image_bytes, mime_type=mime_type)
    encrypted_bytes, encryption = crypto.attachments.encrypt_attachment(image_bytes)
    response = await upload_media_bytes(
        client,
        encrypted_bytes,
        content_type="application/octet-stream",
        filename=f"{filename}.enc",
    )
    mxc_uri = upload_content_uri(response)
    if mxc_uri is None:
        msg = f"Matrix screenshot upload failed: {response}"
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
        size=len(image_bytes),
    )


async def download_encrypted_screenshot(
    client: nio.AsyncClient,
    media: EncryptedDesktopMedia,
) -> bytes:
    """Download, authenticate, and decrypt one desktop screenshot."""
    response = await client.download(media.url)
    if not isinstance(response, nio.DownloadResponse) or not isinstance(response.body, bytes):
        msg = f"Matrix screenshot download failed: {response}"
        raise DesktopMediaError(msg)
    if len(response.body) > MAX_SCREENSHOT_BYTES:
        msg = "Encrypted Matrix screenshot exceeds the desktop media limit."
        raise DesktopMediaError(msg)
    try:
        image_bytes = crypto.attachments.decrypt_attachment(
            response.body,
            media.key,
            media.sha256,
            media.iv,
        )
    except Exception as exc:
        msg = "Matrix screenshot authentication or decryption failed."
        raise DesktopMediaError(msg) from exc
    if len(image_bytes) != media.size:
        msg = "Decrypted Matrix screenshot size does not match authenticated metadata."
        raise DesktopMediaError(msg)
    _validate_image_payload(image_bytes, mime_type=media.mime_type)
    return image_bytes


def _validate_image_payload(image_bytes: bytes, *, mime_type: str) -> None:
    if not image_bytes or len(image_bytes) > MAX_SCREENSHOT_BYTES:
        msg = f"Screenshot must contain between 1 and {MAX_SCREENSHOT_BYTES} bytes."
        raise DesktopMediaError(msg)
    if mime_type == "image/png" and image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    if mime_type == "image/jpeg" and image_bytes.startswith(b"\xff\xd8\xff"):
        return
    msg = "Screenshot bytes do not match their declared PNG or JPEG MIME type."
    raise DesktopMediaError(msg)


__all__ = ["DesktopMediaError", "download_encrypted_screenshot", "upload_encrypted_screenshot"]
