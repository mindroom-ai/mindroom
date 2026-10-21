"""Static prompt-token estimation: what the model sees before any history replay.

Estimates the non-history cost of one prepared request — the rendered system message, the offered tool
definitions, and the current prompt text. ``build_static_token_estimator`` sizes an owned build's rendered system
message and tool definitions as the request sends them; the team path sizes the leader's prompt and every tool it
offers through the same estimator. Nothing here touches history scopes, sessions, or summaries; compaction consumes
these numbers through the resolved execution plan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec

type _ToolDefinition = dict[str, object]


@dataclass(slots=True)
class _StaticTokenEstimator:
    """Request-local static-token estimator that caches the non-prompt cost once."""

    non_prompt_tokens_fn: Callable[[], int]
    _non_prompt_tokens: int | None = field(default=None, init=False)

    def non_prompt_tokens(self) -> int:
        """Estimate the system message and tool tokens, which do not depend on the prompt text."""
        if self._non_prompt_tokens is None:
            self._non_prompt_tokens = self.non_prompt_tokens_fn()
        return self._non_prompt_tokens

    def estimate(self, full_prompt: str) -> int:
        """Estimate the prompt text's tokens plus the non-prompt cost, which is computed once."""
        return estimate_text_tokens(full_prompt) + self.non_prompt_tokens()


def build_static_token_estimator(
    system_message: Message,
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one owned build's rendered prompt and tools.

    Deferred tools stay off the request until tool search loads them, so they are left out of the estimate.
    """

    def non_prompt_tokens() -> int:
        payloads = [
            {key: value for key, value in asdict(tool).items() if value is not None}
            for tool in tools
            if tool.name not in deferred_tool_names
        ]
        return estimate_text_tokens(str(system_message.content or "")) + _definition_tokens(payloads)

    return _StaticTokenEstimator(non_prompt_tokens)


def _definition_tokens(payloads: Sequence[_ToolDefinition]) -> int:
    return len(stable_serialize(list(payloads))) // 4 if payloads else 0
