"""Shared request and response compatibility for Claude provider adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agno.models.message import Message

from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelOutputLimitError, ModelSafeguardRefusalError
from mindroom.logging_config import get_logger
from mindroom.providers.agno_compat_claude import (
    OUTPUT_LIMIT_STOP_REASONS,
    ClaudeProviderSDKCompat,
    stopped_at_output_limit,
    terminal_message,
)
from mindroom.providers.claude_native_compaction import ClaudeNativeCompaction

if TYPE_CHECKING:
    from typing import NoReturn

    from agno.exceptions import ModelProviderError
    from agno.models.response import ModelResponse
    from agno.tools.function import Function, FunctionCall
    from anthropic.types import Message as AnthropicMessage
    from anthropic.types.beta import BetaMessage

logger = get_logger(__name__)

_CLAUDE_SAFEGUARD_STOP_REASON = "refusal"
# Every cut-off response spends the model's whole output budget, so this many cut-off responses in a row
# get corrective results and the next one ends the run.
_OUTPUT_LIMIT_RETRIES = 2
_CUT_OFF_TOOL_CALL_RESULT = (
    "Not run: your previous response hit the output limit and was cut off before it was complete, "
    "so none of its tool calls ran. Split large content across several smaller tool calls, "
    "for example by writing a long file in parts, then continue."
)


class ClaudeProviderCompat(ClaudeProviderSDKCompat, ClaudeNativeCompaction):
    """Apply current Claude request constraints, preserve typed refusals, and recover cut-off tool calls."""

    id: str
    name: str
    tool_message_role: str

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

    def get_function_calls_to_run(
        self,
        assistant_message: Message,
        messages: list[Message],
        functions: dict[str, Function] | None = None,
    ) -> list[FunctionCall]:
        """Answer every tool call of a response the output limit cut off instead of running it.

        The response's last call is incomplete, and a streamed one carries only the arguments that parsed
        before the cut. Each call gets a result telling the model why, so the loop makes another request;
        the third cut-off response in a row ends the run with an error instead.
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
        cut_off_responses = _cut_off_responses_in_a_row(messages, assistant_message.role)
        if cut_off_responses > _OUTPUT_LIMIT_RETRIES:
            msg = (
                f"The output limit cut off {cut_off_responses} of this model's responses in a row "
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


def _cut_off_responses_in_a_row(messages: list[Message], assistant_role: str) -> int:
    """Count the cut-off responses that end ``messages``, back to the last prompt or complete response.

    Mid-turn compaction folds earlier responses into its summary, which starts the count over; a run that
    keeps being cut off then still ends at its model-call cap (``tool_call_budget.py``).
    """
    count = 0
    for message in reversed(messages):
        if message.role == "user":
            break
        if message.role == assistant_role:
            if not stopped_at_output_limit(message):
                break
            count += 1
    return count
