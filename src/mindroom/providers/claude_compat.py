"""Shared request and response compatibility for Claude provider adapters."""

from __future__ import annotations

from contextlib import aclosing, contextmanager
from typing import TYPE_CHECKING, Any

from agno.models.message import Message

from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.llm.errors import ModelOutputLimitError
from mindroom.logging_config import get_logger
from mindroom.providers.agno_compat_claude import (
    OUTPUT_LIMIT_STOP_REASONS,
    ClaudeProviderSDKCompat,
    stopped_at_output_limit,
    terminal_message,
)
from mindroom.providers.claude_native_compaction import ClaudeNativeCompaction
from mindroom.providers.model_http_client import KeepHttpClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from typing import NoReturn

    from agno.exceptions import ModelProviderError
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutputEvent
    from agno.run.team import TeamRunOutputEvent
    from agno.tools.function import Function, FunctionCall
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types.beta import BetaMessage

logger = get_logger(__name__)

_CLAUDE_SAFEGUARD_STOP_REASON = "refusal"
# Every cut-off response spends the model's whole output budget, so a response loop gets this many
# corrective results and the next cut-off ends it.
_OUTPUT_LIMIT_RETRIES = 2
_CUT_OFF_TOOL_CALL_RESULT = (
    "Not run: your previous response hit the output limit and was cut off before it was complete, "
    "so none of its tool calls ran. Split large content across several smaller tool calls, "
    "for example by writing a long file in parts, then continue."
)


class ClaudeProviderCompat(KeepHttpClient, ClaudeProviderSDKCompat, ClaudeNativeCompaction):
    """Apply current Claude request constraints, preserve typed refusals, and recover cut-off tool calls."""

    id: str
    name: str
    tool_message_role: str
    # The cut-off responses of each running response loop, under the identity of its message list.
    _cut_offs: dict[int, int]

    def __post_init__(self) -> None:
        """Start with no response loop running."""
        super().__post_init__()
        self._cut_offs = {}

    def _check_stop_reason(self, provider_response: object) -> None:
        """Raise a typed refusal, and log a response the output limit cut off."""
        message = terminal_message(provider_response)
        if message is None:
            return
        if message.stop_reason in OUTPUT_LIMIT_STOP_REASONS:
            logger.warning(
                "claude_output_limit_reached",
                model_id=self.id,
                stop_reason=message.stop_reason,
                tool_calls=sum(1 for block in message.content if block.type == "tool_use"),
            )
        elif message.stop_reason == _CLAUDE_SAFEGUARD_STOP_REASON:
            logger.warning(
                "claude_safeguard_refusal",
                model_id=self.id,
                stop_reason=message.stop_reason,
            )
            raise ModelSafeguardRefusalError(
                message=MODEL_SAFEGUARD_REFUSAL_MESSAGE,
                model_name=self.name,
                model_id=self.id,
            )

    def _parse_provider_response(
        self,
        response: AnthropicMessage | BetaMessage,
        response_format: dict[str, Any] | type[Any] | None = None,
        **kwargs: object,
    ) -> ModelResponse:
        self._check_stop_reason(response)
        return super()._parse_provider_response(
            response,
            response_format=response_format,
            **kwargs,
        )

    def _parse_provider_response_delta(
        self,
        response: object,
        response_format: dict[str, Any] | type[Any] | None = None,
    ) -> ModelResponse:
        self._check_stop_reason(response)
        return super()._parse_provider_response_delta(
            response,
            response_format=response_format,
        )

    # AGNO_COMPAT: get_function_calls_to_run cannot tell which response loop a response belongs to.
    # Reason: Agno 3.0.9 passes get_function_calls_to_run only the response, the loop's message list and its
    # functions. Counting cut-off responses in that list fails once mid-turn compaction folds them into its
    # summary, and one model can serve concurrent runs, so each async loop entry point, the only ones MindRoom
    # runs, keeps a count under the identity of that list, which compaction rewrites in place.
    # Upstream issue: Tracking gap; no issue proposes giving get_function_calls_to_run its run or loop.
    # Upstream PR: None identified.
    # Remove when: Agno passes get_function_calls_to_run its run or another per-loop context; the retry limit
    # per response loop remains.
    # Coverage: tests/test_claude_output_limit.py::test_compaction_between_cut_offs_does_not_renew_the_retries;
    # tests/test_claude_output_limit.py::test_concurrent_response_loops_count_their_cut_offs_apart.
    @contextmanager
    def _response_loop(self, messages: list[Message]) -> Iterator[None]:
        """Count one response loop's cut-off responses apart from every other loop, for as long as it runs."""
        key = id(messages)
        self._cut_offs[key] = 0
        try:
            yield
        finally:
            self._cut_offs.pop(key, None)

    async def aresponse(self, messages: list[Message], *args: object, **kwargs: object) -> ModelResponse:
        """Run Agno's response loop with its own count of cut-off responses."""
        with self._response_loop(messages):
            return await super().aresponse(messages, *args, **kwargs)  # ty: ignore[unresolved-attribute]

    async def aresponse_stream(
        self,
        messages: list[Message],
        *args: object,
        **kwargs: object,
    ) -> AsyncIterator[ModelResponse | RunOutputEvent | TeamRunOutputEvent]:
        """Stream Agno's response loop with its own count of cut-off responses."""
        with self._response_loop(messages):
            async with aclosing(
                super().aresponse_stream(messages, *args, **kwargs),  # ty: ignore[unresolved-attribute]
            ) as stream:
                async for event in stream:
                    yield event

    def get_function_calls_to_run(
        self,
        assistant_message: Message,
        messages: list[Message],
        functions: dict[str, Function] | None = None,
    ) -> list[FunctionCall]:
        """Answer every tool call of a response the output limit cut off instead of running it.

        The response's last call is incomplete, and a streamed one carries only the arguments that parsed
        before the cut. Each call gets a result telling the model why, so the loop makes another request;
        the loop's third cut-off response ends it with an error instead. Each response loop counts
        afresh, so a run resumed after an approval gets new retries, and so does the skill review that forks
        a finished response's request.
        """
        if not stopped_at_output_limit(assistant_message):
            return super().get_function_calls_to_run(  # ty: ignore[unresolved-attribute]
                assistant_message,
                messages,
                functions,
            )
        messages.extend(
            Message(
                role=self.tool_message_role,
                tool_call_id=tool_call.get("id"),
                tool_name=tool_call.get("function", {}).get("name"),
                content=_CUT_OFF_TOOL_CALL_RESULT,
                tool_call_error=True,
            )
            for tool_call in assistant_message.tool_calls or []
        )
        # A call from outside Agno's async response loops is never stopped by this limit.
        key = id(messages)
        cut_off_responses = self._cut_offs.get(key, 0) + 1
        if key in self._cut_offs:
            self._cut_offs[key] = cut_off_responses
        if cut_off_responses > _OUTPUT_LIMIT_RETRIES:
            msg = (
                f"The output limit cut off {cut_off_responses} of this model's responses in this run "
                "before their tool calls were complete, so the run stopped. Ask for the work in smaller steps, "
                "or raise the model's max_tokens."
            )
            raise ModelOutputLimitError(msg)
        return []

    def _handle_api_error(self, error: Exception) -> NoReturn:
        if isinstance(error, ModelSafeguardRefusalError):
            raise error
        return super()._handle_api_error(error)  # ty: ignore[unresolved-attribute]

    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        return not isinstance(error, ModelSafeguardRefusalError) and super()._is_retryable_error(  # ty: ignore[unresolved-attribute]
            error,
        )
