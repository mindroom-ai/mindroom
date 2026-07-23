"""Turn-scoped encrypted Matrix media that can be resent without local files."""

from __future__ import annotations

from dataclasses import dataclass

from mindroom.attachment_ids import normalize_attachment_id


@dataclass(frozen=True, slots=True)
class RuntimeEncryptedMediaAttachment:
    """One sendable encrypted MXC object retained only for the active tool context."""

    attachment_id: str
    filename: str
    url: str
    key: str
    iv: str
    sha256: str
    mime_type: str
    size: int

    def __post_init__(self) -> None:
        """Reject malformed internal handles before their keys can enter a Matrix event."""
        if normalize_attachment_id(self.attachment_id) != self.attachment_id:
            msg = "Runtime media attachment_id must be a normalized att_* identifier."
            raise ValueError(msg)
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            msg = "Runtime media filename must be one non-empty path component."
            raise ValueError(msg)
        if not self.url.startswith("mxc://"):
            msg = "Runtime media URL must be an mxc:// URI."
            raise ValueError(msg)
        if not all((self.key, self.iv, self.sha256)):
            msg = "Runtime encrypted media requires key, IV, and SHA-256 metadata."
            raise ValueError(msg)
        if "/" not in self.mime_type:
            msg = "Runtime media MIME type is invalid."
            raise ValueError(msg)
        if isinstance(self.size, bool) or not isinstance(self.size, int) or self.size <= 0:
            msg = "Runtime media size must be a positive integer."
            raise ValueError(msg)

    def encrypted_file_content(self) -> dict[str, object]:
        """Return the Matrix encrypted-file object for an ``m.image`` or file event."""
        return {
            "url": self.url,
            "key": {
                "alg": "A256CTR",
                "ext": True,
                "k": self.key,
                "key_ops": ["encrypt", "decrypt"],
                "kty": "oct",
            },
            "iv": self.iv,
            "hashes": {"sha256": self.sha256},
            "v": "v2",
            "mimetype": self.mime_type,
            "size": self.size,
        }

    def tool_payload(self) -> dict[str, object]:
        """Describe the handle to a model without exposing decryption material."""
        return {
            "attachment_id": self.attachment_id,
            "filename": self.filename,
            "kind": self.mime_type.split("/", 1)[0],
            "mime_type": self.mime_type,
            "size_bytes": self.size,
            "ephemeral": True,
            "sendable": True,
        }
