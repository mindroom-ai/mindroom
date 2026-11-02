"""Identify what a model request holds before each of its messages, so an answer records which request it answered.

A request's identity before message ``i`` digests the route's context key (``ModelProvider.context_key``: the model,
its endpoint, and what the route renders besides the messages) and each message before ``i``: its fields a route can
send, and the content of its media rather than their references. A queued-message notice adds nothing, since a reply
only removes one, or moves it last, after the request that held it. An answer whose request held a URL media item, or
a local file that is gone, has no identity from that message on: its content can change between requests unseen.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.llm.messages import is_queued_notice_message, media_items, stable_serialize

if TYPE_CHECKING:
    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest

__all__ = ["REQUEST_IDENTITY_KEY", "RequestDigests"]

# ``Message.provider_data`` key holding the identity of the request an assistant message answered.
REQUEST_IDENTITY_KEY = "mindroom_request"
# The message fields no route sends.
_UNSENT_FIELDS = frozenset({"id", "created_at", "from_history", "metrics"})


class RequestDigests:
    """Digest the requests one loop sends to ``model``, each message's digest kept while it is the same object.

    A message whose media are local files is digested again on every request, so a file rewritten between requests
    changes the identity; ``invalidate`` drops every kept digest after messages were changed in place.
    """

    def __init__(self, model: ModelProvider) -> None:
        self._model = model
        self._kept: dict[int, tuple[Message, bytes | None]] = {}

    def prefixes(self, request: ModelRequest) -> list[str | None]:
        """Return, for each index ``i`` up to ``len(request.messages)``, the identity of what precedes message ``i``.

        None from the first message that has no identity on.
        """
        digest = hashlib.sha256(self._model.context_key(request).encode())
        identities: list[str | None] = [digest.hexdigest()]
        for message in request.messages:
            if identities[-1] is None or (message_digest := self._message_digest(message)) is None:
                identities.append(None)
                continue
            digest.update(message_digest)
            identities.append(digest.hexdigest())
        return identities

    def invalidate(self) -> None:
        """Forget every kept message digest, after the loop's messages were changed in place."""
        self._kept.clear()

    def _message_digest(self, message: Message) -> bytes | None:
        # A kept entry holds its message, so no other message can take its ``id`` while it is kept.
        kept = self._kept.get(id(message))
        if kept is not None:
            return kept[1]
        digest = _digest_of(message)
        if not any(item.filepath for _kind, item in media_items(message)):
            self._kept[id(message)] = (message, digest)
        return digest


def _digest_of(message: Message) -> bytes | None:
    """Return the digest of what ``message`` can give a model, empty for a queued-message notice.

    None when its content cannot be known here: a media item at a URL, or a local file that is gone.
    """
    if is_queued_notice_message(message):
        return b""
    without_media = replace(message, images=None, audio=None, videos=None, files=None)
    fields = {key: value for key, value in without_media.to_dict().items() if key not in _UNSENT_FIELDS}
    media: list[object] = []
    for kind, item in media_items(message):
        identity = _media_identity(item)
        if identity is None:
            return None
        media.append([kind, identity])
    return hashlib.sha256(stable_serialize([fields, media]).encode("utf-8", "surrogatepass")).digest()


def _media_identity(item: Image | Audio | Video | File) -> object | None:
    """Return what identifies one media item's content: its bytes' digest, or its file's path, size, and change time."""
    metadata = item.to_dict(include_content=False)
    content = item.content
    if content is not None:
        data = content.encode("utf-8", "surrogatepass") if isinstance(content, str) else content
        return [metadata, hashlib.sha256(data).hexdigest()]
    if item.filepath is not None:
        try:
            status = Path(item.filepath).stat()
        except OSError:
            return None
        return [metadata, status.st_size, status.st_mtime_ns]
    return None
