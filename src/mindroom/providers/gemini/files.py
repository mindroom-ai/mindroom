"""Gemini Files API uploads for local audio, video, and media too large to send inline.

Only the Gemini API has a Files API; Vertex AI requests inline their media instead.
Each upload is named after its content and type, so a later request for the same attachment reuses the
active upload instead of sending the file again, until the Files API expires it.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

from mindroom.llm.errors import InvalidRequest

if TYPE_CHECKING:
    from pathlib import Path

    from google.genai import Client
    from google.genai.types import File

# Agno's interval between checks while the Files API processes an upload.
_PROCESSING_POLL_SECONDS = 2.0
# The Gemini API answers 403 for a file name that does not exist in the project.
_MISSING_FILE_STATUSES = frozenset({403, 404})
_ALREADY_EXISTS_STATUS = 409
# Files API names are ``files/`` plus at most 40 lowercase alphanumeric characters.
_NAME_LENGTH = 40


def _content_name(path: Path, mime_type: str) -> str:
    """Return the Files API name for the content and MIME type of ``path``; reads the whole file."""
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, lambda: hashlib.sha256(f"{mime_type}\0".encode()))
    return f"files/{digest.hexdigest()[:_NAME_LENGTH]}"


async def uploaded_file_uri(client: Client, path: Path, mime_type: str, *, provider: str) -> str:
    """Return the URI of the active upload of ``path``, uploading it unless the same content is already there.

    SDK API and transport errors propagate for the adapter to normalize; an upload the Files API cannot
    process raises ``InvalidRequest``.
    """
    from google.genai.errors import APIError  # noqa: PLC0415 - provider SDK loads on first request
    from google.genai.types import FileState  # noqa: PLC0415 - provider SDK loads on first request

    name = await asyncio.to_thread(_content_name, path, mime_type)
    file = await _existing_file(client, name)
    if file is None:
        try:
            file = await client.aio.files.upload(file=path, config={"name": name, "mime_type": mime_type})
        except APIError as error:
            if error.code != _ALREADY_EXISTS_STATUS:
                raise
            # A concurrent request uploaded the same content first.
            file = await client.aio.files.get(name=name)
        except (KeyError, ValueError) as error:
            # The SDK raises these when the upload session never returns an upload URL or never finalizes.
            msg = f"The Gemini Files API upload of {path.name} did not complete"
            raise InvalidRequest(msg, provider=provider) from error
    while file.state == FileState.PROCESSING:
        await asyncio.sleep(_PROCESSING_POLL_SECONDS)
        file = await client.aio.files.get(name=name)
    if file.state != FileState.ACTIVE or not file.uri:
        msg = f"The Gemini Files API could not process {path.name}"
        raise InvalidRequest(msg, provider=provider)
    return file.uri


async def _existing_file(client: Client, name: str) -> File | None:
    from google.genai.errors import APIError  # noqa: PLC0415 - provider SDK loads on first request

    try:
        return await client.aio.files.get(name=name)
    except APIError as error:
        if error.code in _MISSING_FILE_STATUSES:
            return None
        raise


__all__ = ["uploaded_file_uri"]
