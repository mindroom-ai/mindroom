"""Temporary lifecycle compatibility for Agno's OpenAI Responses adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agno.exceptions import ModelProviderError
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseInProgressEvent,
    ResponseOutputItemDoneEvent,
)

from mindroom.error_handling import IncompleteResponsesStreamError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from openai.types.responses import ResponseStreamEvent
    from pydantic import BaseModel

_RESPONSE_ITEMS_BUFFER_KEY = "mindroom_response_items"
_LIFECYCLE_ONLY_KEY = "mindroom_stream_lifecycle_only"

# Reason: Agno 3.0.9 accepts EOF without response.completed and publishes a
# continuation response ID on response.created, before the turn is complete.
# Upstream issue: No separate issue identified; the verified fix is tracked by the PR.
# Upstream PR: https://github.com/agno-agi/agno/pull/10135
# Remove when: The pinned Agno release requires response.completed and publishes
# response IDs only for complete turns across sync and async streams.
# Coverage: tests/test_openai_responses_stream.py::test_unsuccessful_stream_raises_without_publishing_response_id;
# tests/test_openai_responses_stream.py::test_completed_text_publishes_response_id_only_at_completion.

# Reason: Agno's generic retry loop can restart a stream after the caller has
# retained partial text or tool-call state, duplicating visible output or calls.
# Upstream issue: No matching issue identified for partial-output retry ownership.
# Upstream PR: None identified; PR #10135 does not settle retry after partial output.
# Remove when: The pinned Agno retry path proves that restarted streams cannot
# reuse caller-visible partial assistant or tool state.
# Coverage: tests/test_openai_responses_stream.py::test_agent_does_not_retry_incomplete_stream;
# tests/test_openai_responses_stream.py::test_agent_still_retries_transient_provider_errors.

# Reason: Agno 3.0.9 gates Responses continuation behind a hard-coded model-name
# predicate, which excludes aliases and compatible Responses endpoints.
# Upstream issue: No matching issue identified; the public capability is proposed by the PR.
# Upstream PR: https://github.com/agno-agi/agno/pull/10075
# Remove when: A pinned Agno release exposes continuation independently of model
# names while still honoring store=False and explicit replay.
# Coverage: tests/test_openai_models.py::test_responses_continue_tool_calls_independently_of_model_name;
# tests/test_openai_models.py::test_explicit_reasoning_respects_disabled_response_storage.


def _stream_error_types(error: BaseException) -> str:
    """Keep causal exception types without exposing provider payloads or URLs."""
    names: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " caused by ".join(names)


class OpenAIResponsesProviderCompat:
    """Enforce complete stream lifecycle while preserving subclass callbacks."""

    id: str
    name: str

    def _using_reasoning_model(self) -> bool:
        """Enable the Responses continuation capability for every model ID."""
        return True

    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        """Reject retry only after an incomplete stream has retained output."""
        return not isinstance(error, IncompleteResponsesStreamError) and super()._is_retryable_error(  # ty: ignore[unresolved-attribute]
            error,
        )

    def invoke_stream(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        """Require a successful terminal event for each provider invocation."""
        completed = False
        yielded = False
        stream = super().invoke_stream(  # ty: ignore[unresolved-attribute]
            messages,
            assistant_message,
            response_format,
            tools,
            tool_choice,
            run_response,
            compress_tool_results,
        )
        try:
            for chunk in stream:
                lifecycle_only = bool(chunk.extra and chunk.extra.pop(_LIFECYCLE_ONLY_KEY, False))
                yielded = yielded or not lifecycle_only
                completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                yield chunk
        except ModelProviderError as error:
            if not yielded:
                if not str(error).strip():
                    error.message = f"OpenAI Responses stream failed ({_stream_error_types(error)})"
                raise
            msg = f"OpenAI Responses stream failed after yielding output ({_stream_error_types(error)})"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from error
        finally:
            cast("Generator[ModelResponse, None, None]", stream).close()
        if not completed:
            msg = "OpenAI Responses stream ended without response.completed"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id)

    async def ainvoke_stream(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> AsyncIterator[ModelResponse]:
        """Require a successful terminal event for each async provider invocation."""
        completed = False
        yielded = False
        stream = super().ainvoke_stream(  # ty: ignore[unresolved-attribute]
            messages,
            assistant_message,
            response_format,
            tools,
            tool_choice,
            run_response,
            compress_tool_results,
        )
        try:
            async for chunk in stream:
                lifecycle_only = bool(chunk.extra and chunk.extra.pop(_LIFECYCLE_ONLY_KEY, False))
                yielded = yielded or not lifecycle_only
                completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                yield chunk
        except ModelProviderError as error:
            if not yielded:
                if not str(error).strip():
                    error.message = f"OpenAI Responses stream failed ({_stream_error_types(error)})"
                raise
            msg = f"OpenAI Responses stream failed after yielding output ({_stream_error_types(error)})"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from error
        finally:
            await cast("AsyncGenerator[ModelResponse, None]", stream).aclose()
        if not completed:
            msg = "OpenAI Responses stream ended without response.completed"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id)

    def _parse_provider_response_delta(
        self,
        stream_event: ResponseStreamEvent,
        assistant_message: Message,
        tool_use: dict[str, Any],
    ) -> tuple[ModelResponse, dict[str, Any]]:
        """Publish response IDs only at completion and preserve output for owner callbacks."""
        response_items = tool_use.pop(_RESPONSE_ITEMS_BUFFER_KEY, {})
        model_response, tool_use = super()._parse_provider_response_delta(  # ty: ignore[unresolved-attribute]
            stream_event,
            assistant_message,
            tool_use,
        )
        if isinstance(stream_event, ResponseCreatedEvent) and model_response.provider_data is not None:
            model_response.provider_data.pop("response_id", None)
        elif isinstance(stream_event, ResponseCompletedEvent):
            model_response.provider_data = {
                **(model_response.provider_data or {}),
                "response_id": stream_event.response.id,
            }
            items = [item.model_dump(mode="json", exclude_none=True) for item in stream_event.response.output]
            if not items:
                items = [response_items[index] for index in sorted(response_items)]
            self._record_completed_responses_output(model_response, items)
            response_items = {}
        if isinstance(stream_event, ResponseOutputItemDoneEvent):
            self._record_provider_only_responses_items(model_response, [stream_event.item])
            if self._should_buffer_responses_output():
                response_items[stream_event.output_index] = stream_event.item.model_dump(mode="json", exclude_none=True)
        if response_items:
            tool_use[_RESPONSE_ITEMS_BUFFER_KEY] = response_items
        if (
            isinstance(stream_event, (ResponseCreatedEvent, ResponseInProgressEvent))
            and not stream_event.response.output
            and not tool_use
            and not any(
                value for name, value in vars(model_response).items() if name not in {"created_at", "event", "role"}
            )
        ):
            model_response.extra = {_LIFECYCLE_ONLY_KEY: True}
        return model_response, tool_use

    def _record_completed_responses_output(
        self,
        model_response: ModelResponse,
        items: list[dict[str, Any]],
    ) -> None:
        """Delegate completed output to the model that owns replay policy."""
        raise NotImplementedError

    def _record_provider_only_responses_items(
        self,
        model_response: ModelResponse,
        output_items: list[Any],
    ) -> None:
        """Delegate omitted provider items to the model that owns replay policy."""
        raise NotImplementedError

    def _should_buffer_responses_output(self) -> bool:
        """Let the model owner decide whether explicit replay needs ordered output."""
        raise NotImplementedError
