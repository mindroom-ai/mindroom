"""Dependency-free serialization of Matrix encrypted-file metadata."""


def encrypted_file_content(
    *,
    url: str,
    key: str,
    iv: str,
    sha256: str,
    mime_type: str,
    size: int,
) -> dict[str, object]:
    """Build the wire shape for an already validated encrypted media reference."""
    return {
        "url": url,
        "key": {
            "alg": "A256CTR",
            "ext": True,
            "k": key,
            "key_ops": ["encrypt", "decrypt"],
            "kty": "oct",
        },
        "iv": iv,
        "hashes": {"sha256": sha256},
        "v": "v2",
        "mimetype": mime_type,
        "size": size,
    }
