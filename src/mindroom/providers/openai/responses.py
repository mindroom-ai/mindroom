"""The Responses API adapter for the OpenAI and Codex routes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import ENCRYPTED_REPLAY_REJECTED, InvalidRequest, ProviderError, RateLimited
from mindroom.llm.provider import NativeCompactionReplay
from mindroom.llm.request_context import with_request_headers
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.assistant_turn import ABNORMAL_STOP_REASONS, TurnTimer, finished_turn
from mindroom.providers.extra_kwargs import sdk_request_body, settings_params
from mindroom.providers.openai.adapter_common import SDKClients, request_settings, with_large_fields_untyped
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
    replays_encrypted_state,
    request_replay,
    request_store,
    responses_request_kwargs,
    responses_stop_reason,
    usage_report,
)
from mindroom.token_counting.families import counter_for_model
from mindroom.token_counting.requests import route_context_key, route_request_tokens
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.usage.limit_store import normalize_codex_scope_key, record_codex_headers

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

__all__ = ["ResponsesAdapter", "recover_sticky_replay"]

# OpenRouter's rejection of encrypted replay items that its routing keeps from the upstream that created them.
_STICKY_PROVIDER_ERROR = "can only be processed by the provider that created it"


def recover_sticky_replay(
    wire: ResponsesWire,
    settings: ResponsesSettings,
    request: ModelRequest,
    error: ProviderError,
) -> ModelRequest | None:
    """Return ``request`` without encrypted replay state after an OpenRouter sticky-upstream failure, else None.

    That failure is a rejection of the encrypted items, or a rate limit left after the transient retries, of a
    request that replayed such items. The resend keeps the request's routing preferences, whatever they are: one
    slug in ``provider.only`` can cover several endpoints, so it does not prove the items came from one. An
    authored ``input`` the body sends in place of the replay would go unchanged, so such a request is not resent.
    """
    params = settings.request_params or {}
    # ``request_params.extra_body`` replaces the settings' ``extra_body`` (``responses_request_kwargs``).
    extra_body = params.get("extra_body", settings.extra_body) or {}
    sticky = error.code == ENCRYPTED_REPLAY_REJECTED or isinstance(error, RateLimited)
    if not sticky or "input" in params or "input" in extra_body:
        return None
    if not replays_encrypted_state(request_replay(wire, request)):
        return None
    return replace(request, strip_encrypted_replay=True)


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
        counter, scope = self.token_counter, self.wire.reset_scope
        size = partial(route_request_tokens, counter, request, start, self._parts, reset_scope=scope)
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
        # Each message replays as its own items, so the answer at ``start`` and what follows it render alone, and only
        # a whole request counts the instructions, tools, and settings (``static``).
        chained = request_replay(self.wire, request).previous_response_id is not None
        rendered = replace(request, portable_replay=True)
        if start:
            rendered = replace(rendered, messages=request.messages[start:])
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

    def _marked(self, failure: ProviderError) -> ProviderError:
        """Mark OpenRouter's rejection of encrypted replay items, as an HTTP 400 or as a failed response's error."""
        if (
            self.wire.reset_scope is not None
            and isinstance(failure, InvalidRequest)
            and (failure.status_code == 400 or failure.provider_code == "400")
            and _STICKY_PROVIDER_ERROR in str(failure).casefold()
        ):
            failure.code = ENCRYPTED_REPLAY_REJECTED
        return failure

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
        sdk_stream = await client.responses.create(**with_large_fields_untyped(kwargs, ("input", "tools")))
        if self.wire.route == "codex":
            headers = getattr(getattr(sdk_stream, "response", None), "headers", None)
            if isinstance(headers, Mapping):
                with suppress(Exception):
                    record_codex_headers(normalize_codex_scope_key(self.settings.codex_home), headers)
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
        kwargs = with_request_headers(await asyncio.to_thread(self._request_kwargs, request))
        accumulator = ResponsesStream(
            provider=self.identity.provider,
            store=request_store(self.wire, request),
            portable_replay=request.portable_replay,
            native_compaction=request.native_compaction,
            conversation_digest=await asyncio.to_thread(conversation_digest, request.messages),
            encrypted_replay_reset=self.wire.reset_scope if request.strip_encrypted_replay else None,
        )
        try:
            client = await self.clients.client()
            await request.on_send(sdk_request_body(kwargs, SDK_OPTION_KEYS))
            events = self._streamed(client, kwargs, accumulator, timer)
            async with closing_async_stream(events):
                async for event in events:
                    yield event
        except sdk_failures() as error:
            failure = self._marked(
                provider_error(error, provider=self.identity.provider, endpoint=self.identity.endpoint),
            )
            # A request that continues a stored response sends only the messages after it.
            replay = request_replay(self.wire, request)
            failure.rejected_media = rejected_media(
                error,
                kwargs,
                replay.history[replay.sent_from :],
                chat=False,
                provider=self.identity.provider,
                endpoint=self.identity.endpoint,
            )
            raise failure from error
        except ProviderError as failure:
            # A response that failed in its stream raises its own error (``ResponsesStream.add``).
            self._marked(failure)
            raise
        if not accumulator.ended:
            # The stream stopped before the response ended, so the turn did not finish.
            return
        parts = accumulator.finish()
        stop_reason = responses_stop_reason(parts.status, parts.incomplete_reason, tool_calls=bool(parts.tool_calls))
        for event in finished_turn(
            self.identity,
            timer,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            stop_reason=stop_reason,
            raw_stop_reason=(parts.incomplete_reason or parts.status if stop_reason in ABNORMAL_STOP_REASONS else None),
        ):
            yield event
