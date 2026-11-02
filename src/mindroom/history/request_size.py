"""Size one model request: from the latest answer whose request it extends, else by the route's estimate.

Every request a reply sends is sized here: the first one before the reply, each one of a tool loop, a resume's, and
a team member's. An answer records the identity of the request it answered (``llm.request_identity``), so a request
whose content before an answer is that answer's request extends it; the provider's reported size of that request,
plus what the route renders for the answer and everything after it, is the request's size. Without such an answer
the route estimates the whole request (``ModelProvider.request_tokens``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.history.native import with_native_history
from mindroom.history.request_messages import request_layout
from mindroom.llm.request_identity import REQUEST_IDENTITY_KEY, RequestDigests
from mindroom.model_usage import message_context_tokens
from mindroom.native_compaction import checkpoint_items

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.types import NativeHistory, ScopeSummary
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest

__all__ = ["RequestFrame", "RequestSize", "RequestSizer", "request_sizer"]


@dataclass(frozen=True, slots=True)
class RequestFrame:
    """What a reply's requests hold around the scope's history: their settings and tools, system messages, and turn."""

    template: ModelRequest
    system_messages: Sequence[Message]
    turn: TurnMessages

    def request(
        self,
        *,
        summary: ScopeSummary | None,
        history: Sequence[Message],
        native: NativeHistory | None = None,
    ) -> ModelRequest:
        """Return the request that replays ``history`` after ``summary``, with ``native``'s provider-history settings."""
        messages = (*self.system_messages, *request_layout(summary=summary, history=history, turn=self.turn))
        return replace(with_native_history(self.template, native), messages=messages)


@dataclass(frozen=True, slots=True)
class RequestSize:
    """One request's size, and whether a provider's reported size of the request it extends anchors it."""

    tokens: int
    reported: bool


@dataclass(frozen=True, slots=True)
class RequestSizer:
    """Size the requests sent to ``model``; ``configured_provider`` is its config's provider, which reads its usage.

    ``digests`` identifies its requests, keeping message digests across the requests of one loop.
    """

    model: ModelProvider
    configured_provider: str | None
    digests: RequestDigests

    def size(self, request: ModelRequest) -> RequestSize:
        """Return the size of ``request``, anchored on the latest answer whose request it extends."""
        anchor = self._anchor(request)
        if anchor is None:
            return RequestSize(self.model.request_tokens(request), reported=False)
        index, reported = anchor
        return RequestSize(reported + self.model.request_tokens(request, start=index), reported=True)

    def _anchor(self, request: ModelRequest) -> tuple[int, int] | None:
        """Return the index and reported size of the latest answer whose request ``request`` extends.

        The latest native checkpoint for the request's route bounds the search: projection replaced the transcript
        before it, which every earlier answer's size, and the checkpoint's own, still describe.
        """
        prefixes = self.digests.prefixes(request)
        route = request.native_compaction.route if request.native_compaction is not None else None
        identity = self.model.identity
        for index in range(len(request.messages) - 1, -1, -1):
            message = request.messages[index]
            if checkpoint_items(message, route):
                return None
            recorded = (message.provider_data or {}).get(REQUEST_IDENTITY_KEY)
            if message.role != "assistant" or recorded is None or recorded != prefixes[index]:
                continue
            context = message_context_tokens(
                message,
                provider=identity.provider,
                configured_provider=self.configured_provider,
                model_id=identity.id,
            )
            if context is not None:
                return index, context.tokens
        return None


def request_sizer(
    model: ModelProvider,
    config: Config,
    model_name: str,
    digests: RequestDigests | None = None,
) -> RequestSizer:
    """Return the sizer of requests to ``model``, configured as ``model_name``; ``digests`` are a loop's own."""
    configured = config.models.get(model_name)
    return RequestSizer(
        model=model,
        configured_provider=configured.provider if configured is not None else None,
        digests=digests if digests is not None else RequestDigests(model),
    )
