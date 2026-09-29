"""Dependency-free serialization of Matrix encrypted-file metadata."""

ENCRYPTED_FILE_VERSION = "v2"
ENCRYPTED_FILE_KEY_ALGORITHM = "A256CTR"
ENCRYPTED_FILE_KEY_TYPE = "oct"


def encrypted_file_content(
    *,
    url: str,
    key: dict[str, object],
    iv: str,
    hashes: dict[str, object],
    mime_type: str,
    size: int,
) -> dict[str, object]:
    """Build the v2 envelope while preserving the caller's complete key and hash metadata."""
    return {
        "url": url,
        "key": key,
        "iv": iv,
        "hashes": hashes,
        "v": ENCRYPTED_FILE_VERSION,
        "mimetype": mime_type,
        "size": size,
    }


def encrypted_file_content_from_values(
    *,
    url: str,
    key: str,
    iv: str,
    sha256: str,
    mime_type: str,
    size: int,
) -> dict[str, object]:
    """Build the wire shape for an already validated encrypted media reference."""
    return encrypted_file_content(
        url=url,
        key={
            "alg": ENCRYPTED_FILE_KEY_ALGORITHM,
            "ext": True,
            "k": key,
            "key_ops": ["encrypt", "decrypt"],
            "kty": ENCRYPTED_FILE_KEY_TYPE,
        },
        iv=iv,
        hashes={"sha256": sha256},
        mime_type=mime_type,
        size=size,
    )
