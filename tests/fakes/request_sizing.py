"""Request sizing for fake providers: a route that sends messages by role, counted with the fake's ``token_counter``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.token_counting.requests import plain_request_parts, route_context_key, route_request_tokens

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelIdentity, ModelRequest, TokenCounter


class PlainRequestSizing:
    """Size and identify requests as ``plain_request_parts`` renders them; the fake defines ``identity`` and ``token_counter``."""

    identity: ModelIdentity
    token_counter: TokenCounter

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        """Estimate ``request`` from message ``start`` on, as a route that sends messages by role renders it."""
        return route_request_tokens(self.token_counter, request, start, plain_request_parts)

    def context_key(self, request: ModelRequest) -> str:
        """Identify the model and the tool definitions of ``request``."""
        return route_context_key(self.identity, self.token_counter, request, plain_request_parts)


__all__ = ["PlainRequestSizing"]
