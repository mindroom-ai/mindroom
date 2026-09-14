"""OpenAI and OpenAI-compatible models with cross-provider tool-call replay support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from agno.exceptions import ModelProviderError
from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from openai.types.responses import ResponseCompletedEvent, ResponseCreatedEvent, ResponseOutputItemDoneEvent

from mindroom.error_handling import IncompleteResponsesStreamError
from mindroom.legacy_openai_tool_replay import repair_legacy_openai_tool_replay
from mindroom.native_compaction import (
    NativeCompactionModel,
    checkpoint_items,
    native_replay_messages,
    record_native_checkpoint,
)
from mindroom.openai_prompt_cache import formatted_input_with_shared_system_prefix, supports_openai_cache_breakpoints
from mindroom.openai_tool_search import (
    formatted_input_with_tool_search_items,
    model_deferred_tool_names,
    record_tool_search_items,
    request_params_with_deferred_tool_search,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.tools.function import Function
    from openai.types.responses import Response, ResponseStreamEvent
    from pydantic import BaseModel


class ChatToolArgumentsCompat:
    """Repair replayed tool calls before OpenAI Chat Completions formatting.

    Mix in ahead of an ``OpenAIChat`` subclass; ``_format_all_messages`` is the
    single choke point for all four request paths.  Deliberately not a
    dataclass and not an ``OpenAIChat`` subclass: either would re-apply
    ``OpenAIChat`` field defaults over provider-specific ones (base URL, name)
    during dataclass field collection.
    """

    def parse_tool_calls(self, tool_calls_data: list[Any]) -> list[dict[str, Any]]:
        """Drop empty slots created when a streamed tool-call index starts above zero."""
        parsed = super().parse_tool_calls(tool_calls_data)  # ty: ignore[unresolved-attribute]
        return [tool_call for tool_call in parsed if isinstance(tool_call.get("function"), dict)]

    def _format_all_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
    ) -> list[dict[str, Any]]:
        """Supply the arguments string required by OpenAI for every tool call."""
        return super()._format_all_messages(  # ty: ignore[unresolved-attribute]  # resolved by the OpenAIChat sibling base
            repair_legacy_openai_tool_replay(messages),
            compress_tool_results,
        )


@dataclass
class MindRoomOpenAIChat(ChatToolArgumentsCompat, OpenAIChat):
    """OpenAI Chat model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAILike(ChatToolArgumentsCompat, OpenAILike):
    """OpenAI-compatible endpoint model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenRouter(ChatToolArgumentsCompat, OpenRouter):
    """OpenRouter model that can replay tool calls from other providers."""


@dataclass
class MindRoomDeepSeek(ChatToolArgumentsCompat, DeepSeek):
    """DeepSeek model that can replay tool calls from other providers."""


@dataclass
class MindRoomLlamaCpp(ChatToolArgumentsCompat, LlamaCpp):
    """llama.cpp server model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAIResponses(NativeCompactionModel, OpenAIResponses):
    """OpenAI Responses model that preserves completed response and tool-search state."""

    approval_receipt_after_response_id: ClassVar[bool] = True
    supports_prompt_cache_breakpoints: ClassVar[bool] = True
    cache_system_prompt: bool = True

    def native_compaction_supported(self) -> bool:
        """Use explicit replay on public Responses and Codex routes."""
        return (
            self.store is not True
            and not self.background
            and self.id.startswith(("gpt-5.3-codex", "gpt-5.4", "gpt-6"))
            and self.native_compaction_endpoint()
            in {
                "https://api.openai.com/v1",
                "https://chatgpt.com/backend-api/codex",
            }
            and not any(
                any(key in params for key in ("context_management", "previous_response_id", "background", "store"))
                for params in (self.request_params or {}, self.extra_body or {})
            )
        )

    def native_compaction_endpoint(self) -> str:
        """Bind replay to the effective client endpoint."""
        if self.async_client is not None:
            return str(self.async_client.base_url).rstrip("/")
        if self.client is not None:
            return str(self.client.base_url).rstrip("/")
        return str(
            (self.client_params or {}).get("base_url")
            or self.base_url
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1",
        ).rstrip("/")

    def configure_native_compaction(self, *, threshold: int | None, history_generation: str = "") -> None:
        """Use self-contained replay when native compaction is enabled."""
        super().configure_native_compaction(threshold=threshold, history_generation=history_generation)
        if self.native_compaction is not None:
            self.store = False

    def __post_init__(self) -> None:
        """Use one storage setting for request construction and history replay."""
        super().__post_init__()
        if self.request_params is not None and "store" in self.request_params:
            self.request_params = dict(self.request_params)
            self.store = self.request_params.pop("store")
        if self.background and self.store is False:
            msg = "Background Responses require store=True"
            raise ValueError(msg)

    def _using_reasoning_model(self) -> bool:
        """Enable Responses continuation independently of the model's name.

        Agno 3.0.9 gates response chaining and encrypted reasoning retrieval on
        this predicate, although both belong to the API rather than a model list.
        This does not enable reasoning or override ``store=False``.
        """
        return True

    def get_request_params(
        self,
        messages: list[Message] | None = None,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
    ) -> dict[str, Any]:
        """Tag deferred functions and add hosted tool search."""
        request_params = super().get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if self.native_compaction is not None:
            request_params["context_management"] = [
                {"type": "compaction", "compact_threshold": self.native_compaction.threshold},
            ]
        return request_params_with_deferred_tool_search(request_params, model_deferred_tool_names(self))

    def _format_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
        tools: list[Function | dict[str, Any]] | None = None,
    ) -> list[Any]:
        """Reinsert captured tool-search items that Agno drops from history."""
        messages = repair_legacy_openai_tool_replay(messages)
        route = self.native_compaction.route if self.native_compaction is not None else None
        messages = native_replay_messages(messages, route)
        formatted_input = super()._format_messages(messages, compress_tool_results, tools=tools)
        checkpoint_index = next(
            (index for index, message in enumerate(messages) if checkpoint_items(message, route)),
            None,
        )
        if checkpoint_index is not None:
            checkpoint = messages[checkpoint_index]
            prefix_size = len(super()._format_messages(messages[:checkpoint_index], compress_tool_results, tools=tools))
            replaced_size = len(super()._format_messages([checkpoint], compress_tool_results, tools=tools))
            formatted_input = [
                *formatted_input[:prefix_size],
                *checkpoint_items(checkpoint, route),
                *formatted_input[prefix_size + replaced_size :],
            ]
            # Native output already includes hosted tool-search items in order.
            messages = [*messages[:checkpoint_index], *messages[checkpoint_index + 1 :]]
        if self.cache_system_prompt:
            formatted_input = formatted_input_with_shared_system_prefix(
                formatted_input,
                explicit_breakpoint=self.supports_prompt_cache_breakpoints and supports_openai_cache_breakpoints(self),
            )
        return formatted_input_with_tool_search_items(messages, formatted_input)

    def _parse_provider_response(self, response: Response, **kwargs: object) -> ModelResponse:
        """Capture tool-search output items that Agno's parser drops."""
        model_response = super()._parse_provider_response(response, **kwargs)
        record_tool_search_items(model_response, response.output)
        if response.status == "completed":
            record_native_checkpoint(
                model_response,
                [item.model_dump(mode="json", exclude_none=True) for item in response.output],
                self.native_compaction,
            )
        return model_response

    # Agno 3.0.9 workaround; upstream completion/response-ID fix:
    # https://github.com/agno-agi/agno/pull/10135
    # Remove duplicate completion/ID checks after pinning a release with that fix.
    # Keep retry protection until Agno also avoids reusing partial stream output.
    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        """Do not retry incomplete streams with Agno's retained partial text and tool calls."""
        return not isinstance(error, IncompleteResponsesStreamError) and super()._is_retryable_error(error)

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
        stream = super().invoke_stream(
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
                yielded = True
                # The parser publishes response_id only on response.completed.
                completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                yield chunk
        except ModelProviderError as error:
            if not yielded:
                raise
            msg = "OpenAI Responses stream failed after yielding output"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from error
        finally:
            # Agno returns a generator, annotated only as Iterator.
            cast("Generator[ModelResponse, None, None]", stream).close()
        if not completed:
            msg = "OpenAI Responses stream ended without response.completed"
            raise IncompleteResponsesStreamError(
                msg,
                model_name=self.name,
                model_id=self.id,
            )

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
        stream = super().ainvoke_stream(
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
                yielded = True
                completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                yield chunk
        except ModelProviderError as error:
            if not yielded:
                raise
            msg = "OpenAI Responses stream failed after yielding output"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from error
        finally:
            # Finalize Agno's async generator when the consumer stops at a yield.
            await cast("AsyncGenerator[ModelResponse, None]", stream).aclose()
        if not completed:
            msg = "OpenAI Responses stream ended without response.completed"
            raise IncompleteResponsesStreamError(
                msg,
                model_name=self.name,
                model_id=self.id,
            )

    def _parse_provider_response_delta(
        self,
        stream_event: ResponseStreamEvent,
        assistant_message: Message,
        tool_use: dict[str, Any],
    ) -> tuple[ModelResponse, dict[str, Any]]:
        """Publish only completed response IDs and capture native tool-search items."""
        native_items = tool_use.pop("mindroom_native_items", [])
        model_response, tool_use = super()._parse_provider_response_delta(stream_event, assistant_message, tool_use)
        if isinstance(stream_event, ResponseCreatedEvent) and model_response.provider_data is not None:
            # An unfinished response may contain tool calls we never received.
            # Chaining to it would require outputs that we cannot supply.
            model_response.provider_data.pop("response_id", None)
        elif isinstance(stream_event, ResponseCompletedEvent):
            model_response.provider_data = {
                **(model_response.provider_data or {}),
                "response_id": stream_event.response.id,
            }
            items = native_items
            if not items:
                items = [item.model_dump(mode="json", exclude_none=True) for item in stream_event.response.output]
            record_native_checkpoint(model_response, items, self.native_compaction)
            native_items = []
        if isinstance(stream_event, ResponseOutputItemDoneEvent):
            record_tool_search_items(model_response, [stream_event.item])
            if self.native_compaction is not None:
                native_items.append(
                    stream_event.item.model_dump(mode="json", exclude_none=True),
                )
        if native_items:
            tool_use["mindroom_native_items"] = native_items
        return model_response, tool_use
