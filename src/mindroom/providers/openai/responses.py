"""The Responses API adapter for the OpenAI and Codex routes.

Codex only streams (its capabilities say ``streams_only``), so a non-streaming request on that route still streams
and returns the drained turn.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import NativeCompactionReplay, StreamProgress, TurnCompleted, UsageReported
from mindroom.native_compaction import native_compaction_route
from mindroom.providers.assistant_turn import finished_assistant_message, output_started
from mindroom.providers.extra_kwargs import settings_params
from mindroom.providers.openai.adapter_common import SDKClients, request_settings
from mindroom.providers.openai.errors import provider_error, sdk_failures
from mindroom.providers.openai.replay_estimate import (
    estimate_responses_replay_tokens,
    responses_replay_uses_visual_tokens,
)
from mindroom.providers.openai.responses_wire import (
    ResponsesStream,
    ResponsesWire,
    parse_response,
    request_store,
    responses_request_kwargs,
    responses_stop_reason,
    usage_report,
)
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from openai import AsyncOpenAI

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import (
        Capabilities,
        ModelIdentity,
        ModelRequest,
        NativeHistoryRoute,
        PortableReplayEstimator,
        StreamEvent,
    )
    from mindroom.providers.openai.settings import ResponsesSettings

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
    def replay_estimator(self) -> PortableReplayEstimator:
        """Responses sizes explicitly replayed history as its requests send it."""
        return self

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

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

    def portable_replay_uses_visual_tokens(self) -> bool:
        """Return whether replayed images are priced by their visual patches."""
        return responses_replay_uses_visual_tokens(self.identity.id)

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int:
        """Count the input an explicitly replayed request sends for ``messages``, for history budgets."""
        return estimate_responses_replay_tokens(
            messages,
            model_id=self.identity.id,
            cache_system_prompt=self.settings.cache_system_prompt,
            explicit_breakpoint=self.wire.explicit_breakpoint,
        )

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        return responses_request_kwargs(
            model_id=self.identity.id,
            request=request,
            settings=request_settings(self.settings, request.overrides, self.capabilities),
            wire=self.wire,
        )

    async def _streamed(
        self,
        client: AsyncOpenAI,
        kwargs: dict[str, Any],
        accumulator: ResponsesStream,
    ) -> AsyncIterator[StreamEvent]:
        sdk_stream = await client.responses.create(**kwargs, stream=True)
        try:
            async for event in sdk_stream:
                if (usage := usage_report(event)) is not None:
                    yield usage
                deltas = accumulator.add(event)
                for delta in deltas:
                    yield delta
                if not deltas:
                    # An event without deltas, such as tool-call arguments, is still activity.
                    yield StreamProgress()
        finally:
            await sdk_stream.close()

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one Responses request and yield its deltas, usage, and finished turn."""
        started = time.monotonic()
        first_token: float | None = None
        kwargs = await asyncio.to_thread(self._request_kwargs, request)
        client = await self.clients.client(request.overrides)
        store = request_store(self.wire, request)
        # TODO(simplify): With `reasoning` authored, the reply text is also stored as its reasoning (streamed only
        # without `reasoning_summary`, blocking only without a returned summary). Store only returned reasoning
        # summaries. Stored data and anything that replays or shows reasoning.
        try:
            if request.stream or self.capabilities.streams_only:
                accumulator = ResponsesStream(
                    provider=self.identity.provider_name,
                    store=store,
                    portable_replay=request.portable_replay,
                    native_compaction=request.native_compaction,
                    text_as_reasoning=self.settings.reasoning is not None and self.settings.reasoning_summary is None,
                )
                events = self._streamed(client, kwargs, accumulator)
                async with closing_async_stream(events):
                    async for event in events:
                        first_token = output_started(first_token, event)
                        yield event
                if not accumulator.ended:
                    # The stream stopped before the response ended, so the turn did not finish.
                    return
                parts = accumulator.finish()
            else:
                response = await client.responses.create(**kwargs)
                parts = parse_response(
                    response,
                    provider=self.identity.provider_name,
                    store=store,
                    portable_replay=request.portable_replay,
                    native_compaction=request.native_compaction,
                    text_as_reasoning=self.settings.reasoning is not None,
                )
                first_token = time.monotonic()
        except sdk_failures() as error:
            raise provider_error(error, provider=self.identity.provider_name) from error
        message = finished_assistant_message(
            self.identity,
            content=parts.content,
            reasoning=parts.reasoning,
            tool_calls=parts.tool_calls,
            provider_data=parts.provider_data,
            usage=parts.usage,
            started=started,
            first_output_at=first_token,
        )
        if parts.usage is not None:
            yield UsageReported(message.metrics)
        stop = responses_stop_reason(parts.status, parts.incomplete_reason, tool_calls=bool(parts.tool_calls))
        yield TurnCompleted(message=message, stop_reason=stop)
