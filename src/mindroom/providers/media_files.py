"""Read and type the attachment files a request carries, with one rule for a file a route cannot send.

Every wire reads attachment files here. A missing or unreadable file raises ``InvalidRequest``, so the media fallback
replaces the request's media with its note instead of the model answering about an attachment it never saw, and an
empty file is left out of the request with a warning, as a part without content would be rejected. Every wire
types a file by ``file_mime_type`` and sends one of a type its API does not read as ``unreadable_file_note``, unread,
so the file costs no rejected request and hides none of the message's other attachments.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from mindroom.llm.errors import InvalidRequest
from mindroom.llm.media import IMAGE_HEADER_BYTES, canonical_mime_type, named_mime_type, url_file_name
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.llm.media import File

__all__ = [
    "OPENAI_TEXT_TYPES",
    "file_bytes",
    "file_mime_type",
    "file_name",
    "file_text",
    "has_file_bytes",
    "is_text_mime_type",
    "read_media_bytes",
    "read_media_file",
    "unreadable_file_note",
]

logger = get_logger(__name__)

_OCTET_STREAM = "application/octet-stream"
# The types outside ``text/`` that OpenAI's file inputs guide (https://developers.openai.com/api/docs/guides/pdf-files)
# lists as text, code, spreadsheets, and rich documents; Responses takes each as declared.
OPENAI_TEXT_TYPES = frozenset(
    {
        "application/csv",
        "application/graphql",
        "application/javascript",
        "application/json",
        "application/json5",
        "application/rtf",
        "application/toml",
        "application/typescript",
        "application/x-awk",
        "application/x-bash",
        "application/x-graphql",
        "application/x-httpd-php",
        "application/x-httpd-php-source",
        "application/x-iif",
        "application/x-json5",
        "application/x-ndjson",
        "application/x-patch",
        "application/x-php",
        "application/x-powershell",
        "application/x-protobuf",
        "application/x-rust",
        "application/x-scala",
        "application/x-sql",
        "application/x-subrip",
        "application/x-terraform",
        "application/x-toml",
        "application/x-yaml",
        "application/yaml",
        "message/rfc822",
    },
)
# Text types outside ``text/``: OpenAI's, without ``application/x-protobuf``, which usually names a binary payload (a
# ``.proto`` source file is typed as text by its name), and common declared types for XML, JavaScript, Python, and
# shell scripts.
_TEXT_MIME_TYPES = (OPENAI_TEXT_TYPES - {"application/x-protobuf"}) | {
    "application/xml",
    "application/x-javascript",
    "application/x-python",
    "application/x-sh",
    "application/x-shellscript",
}


def read_media_file(
    path: str | Path,
    *,
    kind: str,
    provider: str,
    limit: int | None = None,
) -> tuple[int, bytes] | None:
    """Return a local attachment's size and bytes: all of them, or only its header when it is larger than ``limit``.

    None is for an empty file, which the request leaves out. ``kind`` names the attachment in the warning and the
    error, and ``provider`` is the adapter that raises it.
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
        raise InvalidRequest(msg, provider=provider) from error
    if not size:
        logger.warning("media_file_empty", kind=kind, path=str(path))
        return None
    return size, data


def read_media_bytes(path: str | Path, *, kind: str, provider: str) -> bytes | None:
    """Return a local attachment's bytes, or None for an empty file (``read_media_file``)."""
    read = read_media_file(path, kind=kind, provider=provider)
    return None if read is None else read[1]


def file_name(file: File, fallback: str) -> str:
    """Return the name a file goes by: its original filename, else its stored file's name, else ``fallback``.

    A stored attachment file's name holds the attachment ID.
    """
    return file.filename or file.name or (PurePosixPath(file.filepath).name if file.filepath else None) or fallback


def has_file_bytes(file: File) -> bool:
    """Return whether a file carries bytes to send, as a local file or content, rather than only an id or a URL."""
    return file.filepath is not None or file.content is not None


def file_bytes(file: File, *, provider: str) -> bytes | None:
    """Return a file's bytes, or None for a file without any or an empty local file (``read_media_file``)."""
    if file.filepath is not None:
        return read_media_bytes(file.filepath, kind="file", provider=provider)
    return file.content.encode("utf-8") if isinstance(file.content, str) else file.content


def file_mime_type(file: File) -> str:
    """Return a file's type: its declared one, else the one its original filename names, then its stored path or URL.

    The original filename comes before the path because a storage path is opaque and may end in ``.bin``. A declared
    ``application/octet-stream`` names no type, so the names decide then too. A file nothing types is
    ``application/octet-stream``, which no route reads.
    """
    declared = canonical_mime_type(file.mime_type)
    if declared is not None and declared != _OCTET_STREAM:
        return declared
    names = (file_name(file, ""), file.filepath, url_file_name(file.url) if file.url else None)
    return next((named for name in names if name and (named := named_mime_type(name)) is not None), _OCTET_STREAM)


def is_text_mime_type(mime_type: str) -> bool:
    """Return whether ``mime_type``, as ``file_mime_type`` gives it, is a text type."""
    return mime_type.startswith("text/") or mime_type in _TEXT_MIME_TYPES


def file_text(data: bytes) -> str:
    """Return a text file's bytes as text, read as UTF-8, the encoding MindRoom keeps text in.

    Every wire that sends a text file as text reads it here. A byte that is not UTF-8 reads as a replacement character,
    so a file in another encoding, such as a Latin-1 CSV, still reads except for those characters.
    """
    return data.decode("utf-8", errors="replace")


def unreadable_file_note(file: File, mime_type: str) -> str:
    """Return the text a wire sends instead of a file of ``mime_type``, a type its API does not read."""
    return (
        f"[Attachment omitted: {file_name(file, 'file')} ({mime_type}) is a file type this model cannot read. Its content was not "
        "inspected. Use get_attachment without view to inspect or save it, then use other available tools.]"
    )
