"""Shared request and response compatibility for Claude provider adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from anthropic.lib.streaming import MessageStopEvent, ParsedBetaMessageStopEvent, ParsedMessageStopEvent
from anthropic.types import Message as AnthropicMessage
from anthropic.types.beta import BetaMessage

from mindroom.error_handling import MODEL_SAFEGUARD_REFUSAL_MESSAGE, ModelSafeguardRefusalError
from mindroom.logging_config import get_logger
from mindroom.model_defaults import CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES

if TYPE_CHECKING:
    from typing import NoReturn

    from agno.exceptions import ModelProviderError
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_CLAUDE_SAFEGUARD_STOP_REASON = "refusal"
_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")


class ClaudeProviderCompat:
    """Apply current Claude request constraints and preserve typed refusals."""

    id: str
    name: str

    def get_request_params(
        self,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build request parameters accepted by the selected Claude generation."""
        request_params = super().get_request_params(  # ty: ignore[unresolved-attribute]
            response_format=response_format,
            tools=tools,
        )
        if self.id.casefold().endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES):
            for parameter_name in _SAMPLING_CONTROL_NAMES:
                request_params.pop(parameter_name, None)
        return request_params

    def _raise_for_safeguard_refusal(self, provider_response: object) -> None:
        if isinstance(provider_response, (MessageStopEvent, ParsedMessageStopEvent, ParsedBetaMessageStopEvent)):
            stop_reason = provider_response.message.stop_reason
        elif isinstance(provider_response, (AnthropicMessage, BetaMessage)):
            stop_reason = provider_response.stop_reason
        else:
            return
        if stop_reason != _CLAUDE_SAFEGUARD_STOP_REASON:
            return
        logger.warning(
            "claude_safeguard_refusal",
            model_id=self.id,
            stop_reason=stop_reason,
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
        self._raise_for_safeguard_refusal(response)
        return super()._parse_provider_response(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
            **kwargs,
        )

    def _parse_provider_response_delta(
        self,
        response: object,
        response_format: dict[str, Any] | type[Any] | None = None,
    ) -> ModelResponse:
        self._raise_for_safeguard_refusal(response)
        return super()._parse_provider_response_delta(  # ty: ignore[unresolved-attribute]
            response,
            response_format=response_format,
        )

    def _handle_api_error(self, error: Exception) -> NoReturn:
        if isinstance(error, ModelSafeguardRefusalError):
            raise error
        return super()._handle_api_error(error)  # ty: ignore[unresolved-attribute]

    def _is_retryable_error(self, error: ModelProviderError) -> bool:
        return not isinstance(error, ModelSafeguardRefusalError) and super()._is_retryable_error(  # ty: ignore[unresolved-attribute]
            error,
        )
