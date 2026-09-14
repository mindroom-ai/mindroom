"""OpenAI and OpenAI-compatible models with cross-provider tool-call replay support."""

from __future__ import annotations

import base64
import math
import os
import struct
from copy import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, cast

from agno.exceptions import ModelProviderError
from agno.models.deepseek import DeepSeek
from agno.models.llama_cpp import LlamaCpp
from agno.models.openai import OpenAIChat, OpenAIResponses
from agno.models.openai.like import OpenAILike
from agno.models.openrouter import OpenRouter
from agno.utils.media import get_image_type
from agno.utils.tokens import _parse_image_dimensions_from_bytes
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseInProgressEvent,
    ResponseOutputItemDoneEvent,
)

from mindroom.error_handling import IncompleteResponsesStreamError
from mindroom.legacy_openai_tool_replay import repair_legacy_openai_tool_replay
from mindroom.native_compaction import (
    NativeCompactionModel,
    common_native_endpoint,
    native_replay_messages,
    record_native_checkpoint,
    recorded_native_settings,
)
from mindroom.openai_prompt_cache import formatted_input_with_shared_system_prefix, supports_openai_cache_breakpoints
from mindroom.openai_response_replay import (
    formatted_input_with_provider_items,
    record_response_output,
    record_tool_search_items,
)
from mindroom.openai_tool_search import (
    model_deferred_tool_names,
    request_params_with_deferred_tool_search,
)
from mindroom.token_budget import approximate_o200k_tokens, image_content_for_token_estimation, stable_serialize

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator

    from agno.models.message import Message
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.tools.function import Function
    from openai.types.chat import ChatCompletion
    from openai.types.responses import Response, ResponseStreamEvent
    from pydantic import BaseModel


class OpenAIChatProviderCompat:
    """Repair tool replay and preserve OpenAI Chat Completions finish reasons.

    Mix in ahead of an ``OpenAIChat`` subclass; ``_format_all_messages`` is the
    single choke point for all four request paths.  Deliberately not a
    dataclass and not an ``OpenAIChat`` subclass: either would re-apply
    ``OpenAIChat`` field defaults over provider-specific ones (base URL, name)
    during dataclass field collection.
    """

    def _parse_provider_response(self, response: ChatCompletion, **kwargs: object) -> ModelResponse:
        """Retain the terminal reason Agno drops when parsing Chat Completions."""
        parsed = super()._parse_provider_response(response, **kwargs)  # ty: ignore[unresolved-attribute]
        parsed.provider_data = {**(parsed.provider_data or {}), "finish_reason": response.choices[0].finish_reason}
        return parsed

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
class MindRoomOpenAIChat(OpenAIChatProviderCompat, OpenAIChat):
    """OpenAI Chat model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenAILike(OpenAIChatProviderCompat, OpenAILike):
    """OpenAI-compatible endpoint model that can replay tool calls from other providers."""


@dataclass
class MindRoomOpenRouter(OpenAIChatProviderCompat, OpenRouter):
    """OpenRouter model that can replay tool calls from other providers."""


@dataclass
class MindRoomDeepSeek(OpenAIChatProviderCompat, DeepSeek):
    """DeepSeek model that can replay tool calls from other providers."""


@dataclass
class MindRoomLlamaCpp(OpenAIChatProviderCompat, LlamaCpp):
    """llama.cpp server model that can replay tool calls from other providers."""


def _embedded_image_dimensions(source: object) -> tuple[int, int] | None:
    """Read known image headers locally, with no URL fetch or pixel decoding."""
    if not isinstance(source, str) or not source.startswith("data:image/") or ";base64," not in source:
        return None
    try:
        data = base64.b64decode(source.split(",", 1)[1], validate=True)
        # Agno's higher-level image helper can fetch URLs; use only its header parser.
        if get_image_type(data) in {"png", "gif", "jpeg", "webp"}:
            width, height = _parse_image_dimensions_from_bytes(data)
            if width > 0 and height > 0:
                return width, height
    except (ValueError, TypeError, struct.error):
        pass
    return None


def _responses_image_tokens(block: dict[str, Any], model_id: str) -> int:
    """Estimate visual patches, never tokenize encoded image transport as text.

    Current patch-model sizing: https://developers.openai.com/api/docs/guides/images-vision
    Unknown models retain the existing transport estimate until their visual
    accounting is known. Unknown dimensions use the model/detail image ceiling.
    """
    if not model_id.startswith(("gpt-6", "gpt-5.6", "gpt-5.5", "gpt-5.4")):
        return approximate_o200k_tokens(stable_serialize(block))
    detail = block.get("detail", "auto")
    recent = model_id.startswith(("gpt-6", "gpt-5.6"))
    if detail == "auto":
        detail = "high" if model_id.startswith("gpt-5.4") else "original"
    max_dimension, patch_limit = 65_535, 30_000
    if detail == "low":
        max_dimension, patch_limit = (2048, 6144) if model_id.startswith("gpt-5.4") else (512, 256)
    elif detail == "high":
        max_dimension, patch_limit = (65_535 if model_id.startswith("gpt-6") else 2048), 2500
    elif not recent:
        max_dimension, patch_limit = 6000, 10_000

    dimensions = _embedded_image_dimensions(block.get("image_url"))
    if dimensions is None:
        return math.ceil(patch_limit * 1.2)
    width, height = dimensions
    scale = min(1, max_dimension / max(width, height))
    patches = math.ceil(math.ceil(width * scale) / 32) * math.ceil(math.ceil(height * scale) / 32)
    # The resize budget is a conservative upper bound; exact resized coverage
    # can be slightly smaller. Original detail on recent models is not resized
    # to the 30,000-patch rejection limit, so do not hide oversized input.
    if not (recent and detail == "original"):
        patches = min(patches, patch_limit)
    return math.ceil(patches * 1.2)


def _prepare_response_continuation(
    messages: list[Message],
    *,
    explicit_replay: bool = False,
) -> tuple[list[Message], bool]:
    """Avoid chaining to an unstored response or an older incomplete server context."""
    latest = next(
        (
            message.provider_data
            for message in reversed(messages)
            if message.role == "assistant" and message.provider_data and message.provider_data.get("response_id")
        ),
        None,
    )
    if not explicit_replay and (latest is None or latest.get("mindroom_response_stored") is not False):
        return messages, False
    return [
        message.model_copy(
            update={
                "provider_data": {key: value for key, value in message.provider_data.items() if key != "response_id"},
            },
        )
        if message.provider_data and "response_id" in message.provider_data
        else message
        for message in messages
    ], True


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


@dataclass
class MindRoomOpenAIResponses(NativeCompactionModel, OpenAIResponses):
    """OpenAI Responses model that preserves completed continuation and ordered output."""

    approval_receipt_after_response_id: ClassVar[bool] = True
    supports_prompt_cache_breakpoints: ClassVar[bool] = True
    cache_system_prompt: bool = True
    _store_before_native_compaction: bool | None = field(default=None, init=False, repr=False)
    _portable_replay: bool = field(default=False, init=False, repr=False)

    def configure_portable_replay(self, *, enabled: bool = True) -> None:
        """Replay the locally budgeted history without hidden server-side context."""
        self._portable_replay = enabled

    def restore_portable_replay(self, message: Message | None) -> None:
        """Restore exact saved policy, using canonical provenance for legacy responses."""
        data = (message.provider_data or {}) if message is not None else {}
        saved = data.get("mindroom_portable_replay")
        if isinstance(saved, bool):
            self.configure_portable_replay(enabled=saved)
            return
        items = data.get("mindroom_response_output")
        has_ordered_reasoning = (
            isinstance(items, list)
            and all(isinstance(item, dict) for item in items)
            and any(
                item.get("type") == "reasoning"
                and isinstance(encrypted := item.get("encrypted_content"), str)
                and bool(encrypted.strip())
                for item in items
            )
        )
        # Legacy records cannot always reveal the original budget. Preserve
        # stored continuation unless canonical replay has its own provenance.
        self.configure_portable_replay(
            enabled=data.get("mindroom_response_stored") is False
            or (message is not None and recorded_native_settings(message) is not None)
            or has_ordered_reasoning,
        )

    def estimate_portable_replay_tokens(self, messages: list[Message]) -> int:
        """Count the explicit Responses payload used by portable history planning."""
        replay_model = copy(self)
        replay_model._portable_replay = True
        replay_model.native_compaction = None
        formatted = replay_model._format_messages(messages)
        estimated_input = []
        image_tokens = 0
        for item in formatted:
            projected = dict(item)
            for field_name in ("content", "output"):
                content = item.get(field_name)
                if isinstance(content, list):
                    projected[field_name] = [image_content_for_token_estimation(block) for block in content]
                    image_tokens += sum(
                        _responses_image_tokens(block, self.id)
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "input_image"
                    )
            estimated_input.append(projected)
        return approximate_o200k_tokens(stable_serialize(estimated_input)) + image_tokens

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
                for params in (
                    self.request_params or {},
                    self.extra_body or {},
                    (self.request_params or {}).get("extra_body") or {},
                )
            )
        )

    def native_compaction_endpoint(self) -> str:
        """Bind replay to the effective client endpoint."""
        clients = [client for client in (self.async_client, self.client) if client is not None]
        if clients:
            return common_native_endpoint([str(client.base_url).rstrip("/") for client in clients])
        return str(
            (self.client_params or {}).get("base_url")
            or self.base_url
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1",
        ).rstrip("/")

    def configure_native_compaction(
        self,
        *,
        threshold: int | None,
        history_generation: str = "",
        allow_authored: bool = False,
    ) -> None:
        """Use self-contained replay when native compaction is enabled."""
        if self.native_compaction is not None:
            self.store = self._store_before_native_compaction
        super().configure_native_compaction(
            threshold=threshold,
            history_generation=history_generation,
            allow_authored=allow_authored,
        )
        if self.native_compaction is not None:
            self._store_before_native_compaction = self.store
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
        if messages is not None:
            messages, _ = _prepare_response_continuation(messages, explicit_replay=self._portable_replay)
        request_params = super().get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if self._portable_replay:
            request_params.pop("previous_response_id", None)
            if isinstance(extra_body := request_params.get("extra_body"), dict):
                request_params["extra_body"] = {
                    key: value for key, value in extra_body.items() if key != "previous_response_id"
                }
            include = list(request_params.get("include") or [])
            if "reasoning.encrypted_content" not in include:
                include.append("reasoning.encrypted_content")
            request_params["include"] = include
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
        """Reconstruct ordered provider output after canonical history conversion."""
        messages, explicit_replay = _prepare_response_continuation(messages, explicit_replay=self._portable_replay)
        messages = repair_legacy_openai_tool_replay(messages)
        route = self.native_compaction.route if self.native_compaction is not None else None
        messages = native_replay_messages(messages, route)
        # Agno couples encrypted-reasoning replay to response storage. A local
        # view preserves stateless history even when the next response is stored.
        replay_model = copy(self) if explicit_replay else self
        if explicit_replay:
            replay_model.store = False
        formatted_input = OpenAIResponses._format_messages(replay_model, messages, compress_tool_results, tools=tools)
        if replay_model.store is not False:
            # Match Agno's continuation boundary before locating assistant spans.
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if message.role == "assistant" and message.provider_data and "response_id" in message.provider_data:
                    messages = messages[index + 1 :]
                    break
        formatted_input = formatted_input_with_provider_items(
            messages,
            formatted_input,
            native_route=route,
            replay_reasoning=replay_model.store is False,
        )
        if self.cache_system_prompt:
            formatted_input = formatted_input_with_shared_system_prefix(
                formatted_input,
                explicit_breakpoint=self.supports_prompt_cache_breakpoints and supports_openai_cache_breakpoints(self),
            )
        return formatted_input

    def _parse_provider_response(self, response: Response, **kwargs: object) -> ModelResponse:
        """Capture completed provider output and response storage provenance."""
        model_response = super()._parse_provider_response(response, **kwargs)
        model_response.provider_data = {
            **(model_response.provider_data or {}),
            "mindroom_response_stored": self.store is not False,
            "response_status": response.status,
            "incomplete_reason": response.incomplete_details.reason
            if response.incomplete_details is not None
            else None,
        }
        record_tool_search_items(model_response, response.output)
        if response.status == "completed":
            model_response.provider_data["mindroom_portable_replay"] = self._portable_replay
            items = [item.model_dump(mode="json", exclude_none=True) for item in response.output]
            record_native_checkpoint(model_response, items, self.native_compaction)
            if self.store is False or self._portable_replay:
                record_response_output(model_response, items)
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
                lifecycle_only = bool(chunk.extra and chunk.extra.pop("mindroom_stream_lifecycle_only", False))
                yielded = yielded or not lifecycle_only
                # The parser publishes response_id only on response.completed.
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
                lifecycle_only = bool(chunk.extra and chunk.extra.pop("mindroom_stream_lifecycle_only", False))
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
        """Publish completed response IDs and ordered provider output."""
        response_items = tool_use.pop("mindroom_response_items", {})
        model_response, tool_use = super()._parse_provider_response_delta(stream_event, assistant_message, tool_use)
        if isinstance(stream_event, ResponseCreatedEvent) and model_response.provider_data is not None:
            # An unfinished response may contain tool calls we never received.
            # Chaining to it would require outputs that we cannot supply.
            model_response.provider_data.pop("response_id", None)
        elif isinstance(stream_event, ResponseCompletedEvent):
            model_response.provider_data = {
                **(model_response.provider_data or {}),
                "response_id": stream_event.response.id,
                "mindroom_response_stored": self.store is not False,
                "mindroom_portable_replay": self._portable_replay,
            }
            items = [item.model_dump(mode="json", exclude_none=True) for item in stream_event.response.output]
            if not items:
                items = [response_items[index] for index in sorted(response_items)]
            record_native_checkpoint(model_response, items, self.native_compaction)
            if self.store is False or self._portable_replay:
                record_response_output(model_response, items)
            response_items = {}
        if isinstance(stream_event, ResponseOutputItemDoneEvent):
            record_tool_search_items(model_response, [stream_event.item])
            if self.store is False or self._portable_replay:
                response_items[stream_event.output_index] = stream_event.item.model_dump(mode="json", exclude_none=True)
        if response_items:
            tool_use["mindroom_response_items"] = response_items
        if (
            isinstance(stream_event, (ResponseCreatedEvent, ResponseInProgressEvent))
            and not stream_event.response.output
            and not tool_use
            and not any(
                value for name, value in vars(model_response).items() if name not in {"created_at", "event", "role"}
            )
        ):
            # A lifecycle snapshot can already contain output the upstream
            # parser ignores. Only empty snapshots and parsed chunks may retry.
            model_response.extra = {"mindroom_stream_lifecycle_only": True}
        return model_response, tool_use
