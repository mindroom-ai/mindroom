"""Read the local attachment files a request carries, with one rule for a file that cannot be read.

Every wire reads attachment files here. A missing or unreadable file raises ``InvalidRequest`` naming its media item,
so the media fallback replaces that attachment with its note instead of the model answering about an attachment it
never saw, and an empty file is left out of the request with a warning, as a part without content would be rejected.
"""

from __future__ import annotations

import os
from pathlib import Path

from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import IMAGE_HEADER_BYTES
from mindroom.logging_config import get_logger

__all__ = ["read_media_bytes", "read_media_file"]

logger = get_logger(__name__)


def read_media_file(
    path: str | Path,
    *,
    media_id: str,
    kind: str,
    provider: str,
    limit: int | None = None,
) -> tuple[int, bytes] | None:
    """Return a local attachment's size and bytes: all of them, or only its header when it is larger than ``limit``.

    None is for an empty file, which the request leaves out. ``kind`` names the attachment in the warning and the
    error, ``media_id`` is the id of its media item, which the error names as rejected, and ``provider`` is the
    adapter that raises it.
    """
    try:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        with file_path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            data = stream.read() if limit is None or size <= limit else stream.read(IMAGE_HEADER_BYTES)
    except OSError as error:
        logger.warning("media_file_unreadable", kind=kind, path=str(path), error=str(error))
        msg = f"The {kind} attachment could not be read"
        raise InvalidRequest(msg, provider=provider, rejected_media=frozenset({media_id})) from error
    if not size:
        logger.warning("media_file_empty", kind=kind, path=str(path))
        return None
    return size, data


def read_media_bytes(path: str | Path, *, media_id: str, kind: str, provider: str) -> bytes | None:
    """Return a local attachment's bytes, or None for an empty file (``read_media_file``)."""
    read = read_media_file(path, media_id=media_id, kind=kind, provider=provider)
    return None if read is None else read[1]
