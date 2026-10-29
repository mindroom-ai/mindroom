"""Read and type the attachment files a request carries, with one rule for a file a route cannot send.

Every wire reads attachment files here. A missing or unreadable file raises ``InvalidRequest``, so the media fallback
replaces the request's media with its note instead of the model answering about an attachment it never saw. Every wire
types a file by ``file_mime_type`` and sends one of a type its API does not read as ``unreadable_file_note``, unread,
so the file costs no rejected request and hides none of the message's other attachments.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import IMAGE_HEADER_BYTES, canonical_mime_type, url_file_name
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.llm.media import File

__all__ = ["file_mime_type", "is_text_mime_type", "read_media_file", "unreadable_file_note"]

logger = get_logger(__name__)

_OCTET_STREAM = "application/octet-stream"
# Python's MIME database lacks or misnames these document formats; host /etc/mime.types must not decide.
_NAMED_FILE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".sh": "text/x-sh",
}
# Text types outside ``text/``.
_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/x-javascript",
        "application/x-python",
    },
)


def read_media_file(path: str | Path, *, kind: str, provider: str, limit: int | None = None) -> tuple[int, bytes]:
    """Return a local attachment's size and bytes: all of them, or only its header when it is larger than ``limit``.

    ``kind`` names the attachment in the error, and ``provider`` is the adapter that raises it.
    """
    try:
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        with file_path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            return size, stream.read() if limit is None or size <= limit else stream.read(IMAGE_HEADER_BYTES)
    except OSError as error:
        logger.warning("media_file_unreadable", kind=kind, path=str(path), error=str(error))
        msg = f"The {kind} attachment could not be read"
        raise InvalidRequest(msg, provider=provider) from error


def _named_type(name: str) -> str | None:
    """Return the type a file name names; a compressed name, such as ``report.csv.gz``, names none."""
    guessed, encoding = mimetypes.guess_type(name)
    guessed = _NAMED_FILE_TYPES.get(PurePosixPath(name).suffix.lower(), guessed)
    return guessed if encoding is None else None


def file_mime_type(file: File) -> str:
    """Return a file's type: its declared one, else the one its original filename names, then its stored path or URL.

    The original filename comes before the path because a storage path is opaque and may end in ``.bin``. A declared
    ``application/octet-stream`` names no type, so the names decide then too. A file nothing types is
    ``application/octet-stream``, which no route reads.
    """
    declared = canonical_mime_type(file.mime_type)
    if declared is not None and declared != _OCTET_STREAM:
        return declared
    names = (file.filename or file.name, file.filepath, url_file_name(file.url) if file.url else None)
    return next((named for name in names if name and (named := _named_type(name)) is not None), _OCTET_STREAM)


def is_text_mime_type(mime_type: str) -> bool:
    """Return whether ``mime_type``, as ``file_mime_type`` gives it, is a text type."""
    return mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES


def unreadable_file_note(file: File, mime_type: str) -> str:
    """Return the text a wire sends instead of a file of ``mime_type``, a type its API does not read.

    The note names the file by its original filename, else its stored file's name, which holds the attachment ID.
    """
    name = file.filename or file.name or (PurePosixPath(file.filepath).name if file.filepath else None) or "file"
    return (
        f"[Attachment omitted: {name} ({mime_type}) is a file type this model cannot read. Its content was not "
        "inspected. Use get_attachment without view to inspect or save it, then use other available tools.]"
    )
