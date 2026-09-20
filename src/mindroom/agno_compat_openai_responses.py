"""Temporary lifecycle compatibility for Agno's OpenAI Responses adapter."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agno.exceptions import ContextWindowExceededError, ModelAuthenticationError, ModelProviderError
from agno.utils.log import log_warning
from openai import APIStatusError
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseErrorEvent,
    ResponseFailedEvent,
    ResponseInProgressEvent,
    ResponseOutputItemDoneEvent,
)

from mindroom.error_handling import IncompleteResponsesStreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from agno.media import File
    from agno.metrics import MessageMetrics
    from agno.models.message import Message
    from agno.models.openai import OpenAIResponses
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput
    from agno.tools.function import Function
    from openai.types.responses import ResponseInputParam, ResponseStreamEvent, ResponseUsage
    from pydantic import BaseModel

_RESPONSE_ITEMS_BUFFER_KEY = "mindroom_response_items"
_LIFECYCLE_ONLY_KEY = "mindroom_stream_lifecycle_only"

# These supported formats are missing or use a different MIME in Python's
# built-in database; host /etc/mime.types must not decide whether they work.
_RESPONSES_FILE_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".sh": "text/x-sh",
}

# AGNO_COMPAT: Responses streams accept incomplete EOF and publish IDs too early.
# Reason: Agno 3.0.9 accepts EOF without response.completed and publishes a
# continuation response ID on response.created, before the turn is complete.
# Upstream issue: No separate issue identified; the verified fix is tracked by the PR.
# Upstream PR: https://github.com/agno-agi/agno/pull/10135
# Remove when: The pinned Agno release requires response.completed and publishes
# response IDs only for complete turns across sync and async streams.
# Coverage: tests/test_openai_responses_stream.py::test_unsuccessful_stream_raises_without_publishing_response_id;
# tests/test_openai_responses_stream.py::test_completed_text_publishes_response_id_only_at_completion.

# AGNO_COMPAT: Native Responses error events are ignored by Agno's stream parser.
# Reason: Typed error/response.failed events otherwise become incomplete EOF,
# hiding transient failures from the provider retry owner before output starts.
# Own raw SDK streams because Agno's invocation loops do not close unread HTTP
# bodies when parsing fails or consumers close; retain provider request callbacks.
# Upstream issue: No matching issue identified for native stream error events.
# Upstream PR: None identified; the lifecycle PR above does not classify errors.
# Remove when: Agno preserves native failure codes and typed context limits and
# deterministically closes SDK streams on parsing errors and consumer closure.
# Coverage: tests/test_responses_stream_retry.py.

# AGNO_COMPAT: Stream retries can reuse caller-visible partial output.
# Reason: Agno's generic retry loop can restart a stream after the caller has
# retained partial text or tool-call state, duplicating visible output or calls.
# Upstream issue: No matching issue identified for partial-output retry ownership.
# Upstream PR: None identified; PR #10135 does not settle retry after partial output.
# Remove when: The pinned Agno retry path proves that restarted streams cannot
# reuse caller-visible partial assistant or tool state.
# Coverage: tests/test_openai_responses_stream.py::test_agent_does_not_retry_incomplete_stream;
# tests/test_openai_responses_stream.py::test_agent_still_retries_transient_provider_errors.

# AGNO_COMPAT: Responses continuation depends on hard-coded model names.
# Reason: Agno 3.0.9 gates Responses continuation behind a hard-coded model-name
# predicate, which excludes aliases and compatible Responses endpoints.
# Upstream issue: No matching issue identified; the public capability is proposed by the PR.
# Upstream PR: https://github.com/agno-agi/agno/pull/10075
# Remove when: A pinned Agno release exposes continuation independently of model
# names while still honoring store=False and explicit replay.
# Coverage: tests/test_openai_models.py::test_responses_continue_tool_calls_independently_of_model_name;
# tests/test_openai_models.py::test_explicit_reasoning_respects_disabled_response_storage.

# AGNO_COMPAT: Responses usage parsing drops cache-write input tokens.
# Reason: Agno 3.0.9 copies cached and reasoning token details but omits
# OpenAI's input_tokens_details.cache_write_tokens counter.
# Upstream issue: No matching issue identified; this metrics gap is untracked.
# Upstream PR: None identified.
# Remove when: The pinned Agno parser preserves cache-write tokens while still
# accepting provider payloads that predate the newer field.
# Coverage: tests/test_openai_models.py::test_openai_metrics_preserve_sdk_input_details.


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

    # AGNO_COMPAT: Responses guesses file MIME from opaque storage paths first.
    # Reason: Agno 3.0.9 ignores the original filename when filepath is set,
    # sending application/octet-stream for attachments stored with a .bin suffix.
    # Upstream issue: No matching issue identified; filename precedence is untracked.
    # Upstream PR: None identified.
    # Remove when: Agno infers missing MIME from the original filename before the
    # storage path; retain binary MIME for compressed attachments with opaque paths.
    # Coverage: tests/test_openai_models.py::test_responses_file_mime_uses_original_attachment_filename;
    # tests/test_openai_models.py::test_responses_file_mime_preserves_remote_references.
    def _format_file_for_input(self, file: File) -> dict[str, Any] | None:
        """Infer missing inline MIME from the display name without changing history."""
        filename = file.filename or file.name
        if not file.mime_type and not file.url and filename and (file.filepath or file.content):
            mime_type, encoding = mimetypes.guess_type(filename)
            mime_type = _RESPONSES_FILE_MIME_TYPES.get(Path(filename).suffix.lower(), mime_type)
            if mime_type and encoding is None:
                file = file.model_copy(update={"mime_type": mime_type})
        return super()._format_file_for_input(file)  # ty: ignore[unresolved-attribute]

    def _using_reasoning_model(self) -> bool:
        """Enable the Responses continuation capability for every model ID."""
        return True

    def _get_metrics(self, response_usage: ResponseUsage) -> MessageMetrics:
        """Preserve cache-write tokens alongside Agno's other usage counters."""
        metrics = super()._get_metrics(response_usage)  # ty: ignore[unresolved-attribute]
        if input_tokens_details := response_usage.input_tokens_details:
            metrics.cache_write_tokens = input_tokens_details.cache_write_tokens or 0
        return metrics

    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        """Reject retry only after an incomplete stream has retained output."""
        return not isinstance(error, IncompleteResponsesStreamError) and super()._is_retryable_error(  # ty: ignore[unresolved-attribute]
            error,
        )

    def _stream_request_params(
        self,
        messages: list[Message],
        response_format: dict[Any, Any] | type[BaseModel] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        run_response: RunOutput | None,
    ) -> dict[str, Any]:
        """Reuse provider request construction while retaining Agno's streaming policy."""
        params = cast("OpenAIResponses", self).get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if params.pop("background", None):
            log_warning("Background mode is not supported for streaming requests. Ignoring `background=True`.")
        return params

    def _stream_input(
        self,
        messages: list[Message],
        compress_tool_results: bool,
        tools: list[dict[str, Any]] | None,
    ) -> ResponseInputParam:
        """Keep subclass formatting while adapting Agno's broad input annotation."""
        return cast(
            "ResponseInputParam",
            cast("OpenAIResponses", self)._format_messages(
                messages,
                compress_tool_results,
                tools=cast("list[Function | dict[str, Any]] | None", tools),
            ),
        )

    def _stream_provider_error(self, error: Exception) -> ModelProviderError:
        """Preserve provider statuses and SDK causes without copying Agno's repeated catches."""
        if isinstance(error, ModelProviderError):
            return error
        status = error.status_code if isinstance(error, APIStatusError) else 502
        error_type = (
            ContextWindowExceededError
            if isinstance(error, APIStatusError) and error.code == "context_length_exceeded"
            else ModelProviderError
        )
        message = error.message if isinstance(error, APIStatusError) else str(error)
        return error_type(message=message, status_code=status, model_name=self.name, model_id=self.id)

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
        """Own the SDK stream until completion, failure, or consumer closure."""
        completed = False
        yielded = False
        model = cast("OpenAIResponses", self)
        tool_use: dict[str, Any] = {}
        try:
            params = self._stream_request_params(messages, response_format, tools, tool_choice, run_response)
            assistant_message.metrics.start_timer()
            with model.get_client().responses.create(
                **model._get_model_request_kwargs(),
                input=self._stream_input(messages, compress_tool_results, tools),
                stream=True,
                **params,
            ) as stream:
                for event in stream:
                    chunk, tool_use = self._parse_provider_response_delta(event, assistant_message, tool_use)
                    lifecycle_only = bool(chunk.extra and chunk.extra.pop(_LIFECYCLE_ONLY_KEY, False))
                    yielded = yielded or not lifecycle_only
                    completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                    yield chunk
        except ModelAuthenticationError:
            raise
        except Exception as cause:
            error = self._stream_provider_error(cause)
            if not yielded:
                if not str(error).strip():
                    error.message = f"OpenAI Responses stream failed ({_stream_error_types(cause)})"
                if error is cause:
                    raise
                raise error from cause
            msg = f"OpenAI Responses stream failed after yielding output ({_stream_error_types(cause)})"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from cause
        finally:
            assistant_message.metrics.stop_timer()
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
        """Own the async SDK stream until completion, failure, or cancellation."""
        completed = False
        yielded = False
        model = cast("OpenAIResponses", self)
        tool_use: dict[str, Any] = {}
        try:
            params = self._stream_request_params(messages, response_format, tools, tool_choice, run_response)
            assistant_message.metrics.start_timer()
            async with await model.get_async_client().responses.create(
                **model._get_model_request_kwargs(),
                input=self._stream_input(messages, compress_tool_results, tools),
                stream=True,
                **params,
            ) as stream:
                async for event in stream:
                    chunk, tool_use = self._parse_provider_response_delta(event, assistant_message, tool_use)
                    lifecycle_only = bool(chunk.extra and chunk.extra.pop(_LIFECYCLE_ONLY_KEY, False))
                    yielded = yielded or not lifecycle_only
                    completed = completed or bool(chunk.provider_data and chunk.provider_data.get("response_id"))
                    yield chunk
        except ModelAuthenticationError:
            raise
        except Exception as cause:
            error = self._stream_provider_error(cause)
            if not yielded:
                if not str(error).strip():
                    error.message = f"OpenAI Responses stream failed ({_stream_error_types(cause)})"
                if error is cause:
                    raise
                raise error from cause
            msg = f"OpenAI Responses stream failed after yielding output ({_stream_error_types(cause)})"
            raise IncompleteResponsesStreamError(msg, model_name=self.name, model_id=self.id) from cause
        finally:
            assistant_message.metrics.stop_timer()
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
        if isinstance(stream_event, (ResponseErrorEvent, ResponseFailedEvent)):
            failure = stream_event if isinstance(stream_event, ResponseErrorEvent) else stream_event.response.error
            code = failure.code if failure is not None else None
            message = failure.message if failure is not None else "OpenAI Responses stream failed"
            status = (
                {"server_error": 500, "rate_limit_exceeded": 429, "vector_store_timeout": 504}.get(code, 400)
                if isinstance(code, str)
                else 400
            )
            error_type = ContextWindowExceededError if code == "context_length_exceeded" else ModelProviderError
            raise error_type(message=message, status_code=status, model_name=self.name, model_id=self.id)
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
