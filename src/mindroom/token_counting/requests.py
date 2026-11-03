"""Size one model request the way the route that sends it renders it.

Each route maps the body its own builder renders for a request to ``RequestParts``: the text its model reads, counted
as text; the values its model reads as JSON, such as tool arguments and tool schemas, counted serialized; and what
counts by its cost instead of its characters, such as media and reasoning sent as ciphertext. Transport (ids, roles,
signatures, media bytes) counts nothing. ``route_request_tokens`` and ``route_context_key`` run a route's map, so a
route's size and its identity read one rendering.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.llm.messages import stable_serialize
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelIdentity, ModelRequest, TokenCounter

__all__ = [
    "MEDIA_ALLOWANCE_TOKENS",
    "RequestParts",
    "plain_request_parts",
    "route_context_key",
    "route_request_tokens",
]

# What one media item counts while its real cost is unknown: an image, audio clip, video, or file a request sends.
MEDIA_ALLOWANCE_TOKENS = 5_000


@dataclass(slots=True)
class RequestParts:
    """What one request gives a route's model, counted with ``counter``: text, JSON values, and costs, in body order.

    ``flags`` identify settings that change how the route renders messages, for the request's identity only.
    """

    counter: TokenCounter
    pieces: list[str] = field(default_factory=list)
    tokens: int = 0
    flags: list[object] = field(default_factory=list)

    def text(self, value: str | None) -> None:
        """Count ``value`` as text the model reads."""
        if value:
            self.pieces.append(value)

    def value(self, value: object) -> None:
        """Count ``value`` as JSON the model reads, serialized."""
        if value not in (None, "", [], {}):
            self.pieces.append(stable_serialize(value))

    def cost(self, tokens: int) -> None:
        """Count ``tokens`` the provider charges for something its characters do not measure."""
        self.tokens += tokens

    def flag(self, value: object) -> None:
        """Record a setting that changes how the route renders messages."""
        self.flags.append(value)

    def counted(self) -> int:
        """Return the tokens these parts hold."""
        return count(self.counter, "".join(self.pieces)) + self.tokens


# A route's map: add what ``request`` gives its model to the parts, its messages from ``start`` on, and, from 0, the
# system prompt, tools, and settings too. The request holds no media but what the route prices itself.
type _PartsOf = Callable[["ModelRequest", int, RequestParts], None]


def _media_allowance_tokens(messages: Sequence[Message], *, keep_images: bool = False) -> int:
    """Return the allowance of the media ``messages`` carry, images left out when the route prices them itself."""
    items = sum(len(message.files or ()) + len(message.audio or ()) + len(message.videos or ()) for message in messages)
    if not keep_images:
        items += sum(len(message.images or ()) for message in messages)
    return items * MEDIA_ALLOWANCE_TOKENS


def _without_media(messages: Sequence[Message], *, keep_images: bool = False) -> list[Message]:
    """Return ``messages`` without the media the allowance prices, so a route's map renders no media bytes."""
    stripped: list[Message] = []
    for message in messages:
        updates = {
            name: None
            for name, items in (
                ("files", message.files),
                ("audio", message.audio),
                ("videos", message.videos),
                ("images", None if keep_images else message.images),
            )
            if items
        }
        stripped.append(replace(message, **updates) if updates else message)
    return stripped


def route_request_tokens(
    counter: TokenCounter,
    request: ModelRequest,
    start: int,
    parts_of: _PartsOf,
    *,
    keep_images: bool = False,
) -> int:
    """Return what ``request`` gives the route's model from message ``start`` on, as ``parts_of`` renders it.

    Media are priced at their allowance; ``keep_images`` leaves the images to a route that prices them itself.
    """
    parts = RequestParts(counter)
    parts.cost(_media_allowance_tokens(request.messages[start:], keep_images=keep_images))
    parts_of(replace(request, messages=_without_media(request.messages, keep_images=keep_images)), start, parts)
    return parts.counted()


def route_context_key(identity: ModelIdentity, counter: TokenCounter, request: ModelRequest, parts_of: _PartsOf) -> str:
    """Identify what ``request`` gives the route's model besides its messages, as ``parts_of`` renders it.

    That is the model, its endpoint, the system prompt, tools, and settings the route renders, the flags that change
    how it renders messages, and each deferred tool's definition, which renders nothing until a tool search loads it.
    """
    parts = RequestParts(counter)
    parts_of(replace(request, messages=()), 0, parts)
    deferred = [
        [tool.name, tool.description, tool.parameters, tool.strict]
        for tool in request.tools
        if tool.name in request.deferred_tools.names
    ]
    digest = hashlib.sha256()
    for piece in (
        identity.id,
        identity.provider,
        identity.endpoint or "",
        *parts.pieces,
        stable_serialize(parts.flags),
        stable_serialize(deferred),
    ):
        digest.update(piece.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def plain_request_parts(request: ModelRequest, start: int, parts: RequestParts) -> None:
    """Add a request as a route that sends messages by role renders it: text, tool calls, and tool definitions.

    It is the map of a route whose requests go to no provider, such as the synthetic model; a deferred tool counts
    nothing.
    """
    for message in request.messages[start:]:
        if isinstance(message.content, str):
            parts.text(message.content)
        else:
            parts.value(message.content)
        parts.value(message.tool_calls)
    if not start:
        deferred = request.deferred_tools.names
        parts.value(
            [
                {key: value for key, value in asdict(tool).items() if value is not None}
                for tool in request.tools
                if tool.name not in deferred
            ],
        )
