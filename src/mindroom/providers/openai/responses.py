"""The Responses API adapter for the OpenAI and Codex routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import InvalidRequest
from mindroom.llm.provider import NativeCompactionReplay
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.assistant_turn import TurnTimer, finished_turn
from mindroom.providers.extra_kwargs import sdk_request_body, settings_params
from mindroom.providers.openai.adapter_common import SDKClients, request_settings
from mindroom.providers.openai.chat_wire import SDK_OPTION_KEYS
from mindroom.providers.openai.errors import provider_error, rejected_media, sdk_failures
from mindroom.providers.openai.replay_estimate import (
    responses_body_parts,
    responses_replay_uses_visual_tokens,
)
from mindroom.providers.openai.responses_wire import (
    ResponsesStream,
    ResponsesWire,
    conversation_digest,
    request_replay,
    request_store,
    responses_request_kwargs,
    responses_stop_reason,
    usage_report,
)
from mindroom.token_counting.families import counter_for_model
from mindroom.token_counting.requests import route_context_key, route_request_tokens
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai import AsyncOpenAI

    from mindroom.llm.provider import (
        Capabilities,
        ModelIdentity,
        ModelRequest,
        NativeHistoryRoute,
        StreamEvent,
        TokenCounter,
    )
    from mindroom.providers.openai.settings import ResponsesSettings
    from mindroom.token_counting.requests import RequestParts

__all__ = ["ResponsesAdapter"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesAdapter:
    """One model behind the OpenAI Responses API or the Codex endpoint."""

    identity: ModelIdentity
    capabilities: Capabilities
    wire: ResponsesWire
    settings: ResponsesSettings
    clients: SDKClients

    @property
    def native_history_route(self) -> NativeHistoryRoute:
        """Responses keeps provider-side state, so every request carries its replay policy and compaction settings."""
        return self

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """OpenAI's own endpoint and Codex serve OpenAI's models, which tiktoken counts; a proxy may serve any model."""
        return counter_for_model(
            self.identity,
            local_encoder=self.identity.endpoint == OPENAI_BASE_URL_DEFAULT or self.wire.route == "codex",
        )

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        """Return the replay settings of a request that wants compaction at ``threshold`` input tokens.

        Responses has no authored compaction policy MindRoom can replay, so ``allow_authored`` changes nothing;
        a route without native compaction, or no threshold, runs without it.
        """
        del allow_authored
        if threshold is None or not self.capabilities.native_compaction:
            return None
        # Native compaction is supported only on the public endpoints, so a route that has it has one.
        assert self.identity.endpoint is not None
        route = native_compaction_route(
            provider=self.identity.provider,
            model_id=self.identity.id,
            endpoint=self.identity.endpoint,
            history_generation=history_generation,
        )
        return NativeCompactionReplay(route=route, threshold=threshold)

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        """Estimate the model context ``request`` gives the model, from the body ``stream`` would send to replay it whole.

        A request that continues a stored response sends only what follows it, but the model reads the whole history.
        Images count by their visual patches on a model priced by them, read from each image's header; an image file
        that cannot be read is sent as the media fallback's note, so it counts the media allowance instead.
        """
        size = partial(route_request_tokens, self.token_counter, request, start, self._parts)
        if not responses_replay_uses_visual_tokens(self.identity.id):
            return size()
        try:
            return size(keep_images=True)
        except InvalidRequest:
            return size()

    def context_key(self, request: ModelRequest) -> str:
        """Identify the instructions, tools, and settings the body ``stream`` sends for ``request``."""
        return route_context_key(self.identity, self.token_counter, request, self._parts)

    def _parts(self, request: ModelRequest, start: int, parts: RequestParts) -> None:
        # Each message replays as its own items, so the answer at ``start`` and what follows it render alone.
        chained = request_replay(self.wire, request).previous_response_id is not None
        rendered = replace(
            request if not start else replace(request, messages=request.messages[start:], tools=(), output_schema=None),
            portable_replay=True,
        )
        body = sdk_request_body(self._request_kwargs(rendered), SDK_OPTION_KEYS)
        responses_body_parts(
            body,
            request_replay(self.wire, rendered).history,
            parts,
            model_id=self.identity.id,
            chained=chained,
            static=not start,
        )
        parts.flag(request.native_compaction.route if request.native_compaction is not None else None)

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        return responses_request_kwargs(
            model_id=self.identity.id,
            request=request,
            settings=request_settings(self.settings, request.overrides, self.capabilities),
            wire=self.wire,
            provider=self.identity.provider,
        )

    async def _streamed(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        accumulator: ResponsesStream,
        timer: TurnTimer,
    ) -> AsyncIterator[StreamEvent]:
        sdk_stream = await client.responses.create(**kwargs)
        try:
            async for event in sdk_stream:
                if (usage := usage_report(event)) is not None:
                    yield usage
                for delta in timer.chunk(accumulator.add(event)):
                    yield delta
        finally:
            await sdk_stream.close()

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Responses request and yield its deltas, usage, and finished turn."""
        timer = TurnTimer()
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        accumulator = ResponsesStream(
            provider=self.identity.provider,
            store=request_store(self.wire, request),
            portable_replay=request.portable_replay,
            native_compaction=request.native_compaction,
            conversation_digest=conversation_digest(request.messages),
        )
        try:
            client = await self.clients.client()
            await request.on_send(sdk_request_body(kwargs, SDK_OPTION_KEYS))
            events = self._streamed(client, kwargs, accumulator, timer)
            async with closing_async_stream(events):
                async for event in events:
                    yield event
        except sdk_failures() as error:
            failure = provider_error(error, provider=self.identity.provider)
            # A request that continues a stored response sends only the messages after it.
            replay = request_replay(self.wire, request)
            failure.rejected_media = rejected_media(error, kwargs, replay.history[replay.sent_from :], chat=False)
            raise failure from error
        if not accumulator.ended:
            # The stream stopped before the response ended, so the turn did not finish.
            return
        parts = accumulator.finish()
        for event in finished_turn(
            self.identity,
            timer,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            stop_reason=responses_stop_reason(parts.status, parts.incomplete_reason, tool_calls=bool(parts.tool_calls)),
        ):
            yield event
