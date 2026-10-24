"""Static prompt-token estimation: the rendered system message, the offered tool definitions, and the prompt text.

Compaction consumes these numbers through the resolved execution plan; an agent and a team leader size their
requests through the same estimator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
from typing import TYPE_CHECKING

from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec


@dataclass(frozen=True, slots=True)
class _StaticTokenEstimator:
    """Request-local static-token estimator."""

    # The system message and tool tokens, which do not depend on the prompt text; computed once, on first use.
    non_prompt_tokens: Callable[[], int]

    def estimate(self, full_prompt: str) -> int:
        """Estimate the prompt text's tokens plus the non-prompt cost."""
        return estimate_text_tokens(full_prompt) + self.non_prompt_tokens()


def build_static_token_estimator(
    system_message: Message,
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one rendered system prompt and its tools.

    Deferred tools stay off the request until tool search loads them, so they are left out of the estimate.
    """

    def non_prompt_tokens() -> int:
        tool_tokens = estimate_text_tokens(stable_serialize(tool_definitions(tools, deferred_tool_names)))
        return estimate_text_tokens(str(system_message.content or "")) + tool_tokens

    return _StaticTokenEstimator(cache(non_prompt_tokens))


def tool_definitions(tools: Sequence[ToolSpec], deferred_tool_names: frozenset[str]) -> list[dict[str, object]]:
    """Return the fields each tool definition a request carries in full sets; tool search keeps deferred ones out."""
    return [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in tools
        if tool.name not in deferred_tool_names
    ]
