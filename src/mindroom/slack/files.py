"""Slack file transfers: downloads from Slack's file host into a workspace, and Slack's external upload flow.

Each function takes one call's HTTP client and the token it acts with; a refusal raises `SlackFileError`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import open_directory_within_root, resolve_path_within_root
from mindroom.slack.api import slack_call

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import httpx

# Downloads and uploads alike.
MAX_FILE_BYTES = 50 * 1024 * 1024
_DOWNLOAD_DIR = "slack-downloads"
# Slack sends the token only to its own file host, which serves an authorized download without a redirect.
_DOWNLOAD_HOST = "files.slack.com"
_MAX_NAME_CHARACTERS = 150
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._ -]")
_NOT_THE_FILE = "Slack returned a page instead of the file; reconnect Slack and try again"
# Canvases and posts are Slack documents, which no download link serves as a file.
_DOCUMENT_REFUSAL = "Slack canvases and posts can't be downloaded as files; open them in Slack"
_DOCUMENT_MIMETYPE = "application/vnd.slack-docs"
_DOCUMENT_FILETYPES = frozenset({"quip", "canvas", "space"})


class SlackFileError(Exception):
    """A file transfer refused before or instead of finishing; `str()` is the message for the agent."""


def _is_html(media_type: object) -> bool:
    return isinstance(media_type, str) and media_type.split(";", 1)[0].strip().lower() == "text/html"


def _is_document(file: Mapping[str, Any]) -> bool:
    filetype = file.get("filetype")
    return file.get("mimetype") == _DOCUMENT_MIMETYPE or (isinstance(filetype, str) and filetype in _DOCUMENT_FILETYPES)


async def fetch_file(client: httpx.AsyncClient, token: str, file: Mapping[str, Any]) -> bytearray:
    """Return the bytes of a file `files.info` described, from Slack's file host, refusing them once they pass 50 MB.

    Slack answers a token it does not accept for files with a web page instead of the file, so an HTML answer for a file
    that is not HTML, or a byte count other than the file's size, is refused.
    A Slack document, a canvas or a post, is refused before any request, because nothing tells its export from such a page.
    """
    if _is_document(file):
        raise SlackFileError(_DOCUMENT_REFUSAL)
    link = file.get("url_private_download") or file.get("url_private")
    url = link if isinstance(link, str) else ""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != _DOWNLOAD_HOST:
        msg = "Slack returned no download link for this file"
        raise SlackFileError(msg)
    headers = {"Authorization": f"Bearer {token}"}
    async with client.stream("GET", url, headers=headers) as response:
        if not response.is_success:
            msg = f"Slack refused the download with HTTP {response.status_code}"
            raise SlackFileError(msg)
        if _is_html(response.headers.get("content-type")) and not _is_html(file.get("mimetype")):
            raise SlackFileError(_NOT_THE_FILE)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > MAX_FILE_BYTES:
                msg = f"The file is larger than {MAX_FILE_BYTES} bytes"
                raise SlackFileError(msg)
    size = file.get("size")
    if isinstance(size, int) and len(content) != size:
        raise SlackFileError(_NOT_THE_FILE)
    return content


def _download_name(file_id: str, name: object) -> str:
    """Return one safe file name: the file ID, then Slack's name with anything but plain characters replaced."""
    safe = _UNSAFE_NAME_CHARACTERS.sub("_", name if isinstance(name, str) else "")[:_MAX_NAME_CHARACTERS].strip()
    return f"{file_id}-{safe or 'file'}"


def save_download(workspace_root: Path, file_id: str, name: object, content: bytearray) -> Path:
    """Write a downloaded file atomically into the workspace's `slack-downloads` directory, following no links.

    The saved name starts with the file ID, so a later download of the same file replaces that copy.
    """
    filename = _download_name(file_id, name)
    target = workspace_root.absolute() / _DOWNLOAD_DIR / filename
    try:
        resolve_path_within_root(workspace_root, target, symlinks="reject")
        with (
            open_directory_within_root(workspace_root, _DOWNLOAD_DIR, create=True) as directory_fd,
            atomic_write_file_at(directory_fd, filename) as handle,
        ):
            handle.write(content)
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
        msg = f"The download could not be written to {_DOWNLOAD_DIR}: {reason}"
        raise SlackFileError(msg) from None
    return target


async def _send_upload(client: httpx.AsyncClient, url: str, content: bytes) -> None:
    """Send the file's bytes to the presigned link Slack returned, which takes no token, so none is sent."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not (parts.hostname or "").endswith(".slack.com"):
        msg = "Slack returned an upload link outside slack.com, so the file was not sent"
        raise SlackFileError(msg)
    response = await client.post(url, content=content)
    if not response.is_success:
        msg = f"Slack refused the upload with HTTP {response.status_code}"
        raise SlackFileError(msg)


async def share_file(
    client: httpx.AsyncClient,
    token: str,
    *,
    filename: str,
    content: bytes,
    channel_id: str,
    thread_ts: str | None,
    title: str,
    initial_comment: str | None,
) -> str:
    """Share one file in a conversation, or in the thread of `thread_ts`, and return its file ID.

    Slack's external upload flow: ask for a presigned link, send the bytes there, then share the file.
    """
    link = await slack_call(client, "files.getUploadURLExternal", token, filename=filename, length=len(content))
    upload_url, file_id = link.get("upload_url"), link.get("file_id")
    if not isinstance(upload_url, str) or not isinstance(file_id, str):
        msg = "Slack returned no upload link for this file"
        raise SlackFileError(msg)
    await _send_upload(client, upload_url, content)
    await slack_call(
        client,
        "files.completeUploadExternal",
        token,
        files=[{"id": file_id, "title": title}],
        channel_id=channel_id,
        thread_ts=thread_ts,
        initial_comment=initial_comment,
    )
    return file_id
