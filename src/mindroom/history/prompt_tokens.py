"""Static prompt-token estimation: the rendered system messages, the offered tool definitions, and the prompt text.

Compaction consumes these numbers through the resolved execution plan; an agent and a team leader size their
requests through the same estimator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cache
from typing import TYPE_CHECKING

from mindroom.llm.messages import system_text
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ToolSpec


@dataclass(frozen=True, slots=True)
class _StaticTokenEstimator:
    """Request-local static-token estimator."""

    # The system messages' and tools' tokens, which do not depend on the prompt text; computed once, on first use.
    non_prompt_tokens: Callable[[], int]

    def estimate(self, full_prompt: str) -> int:
        """Estimate the prompt text's tokens plus the non-prompt cost."""
        return estimate_text_tokens(full_prompt) + self.non_prompt_tokens()


def build_static_token_estimator(
    system_messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return a request-local static-token estimator for one rendered system prompt and its tools.

    Deferred tools stay off the request until tool search loads them, so they are left out of the estimate.
    """

    def non_prompt_tokens() -> int:
        return estimate_text_tokens(system_text(system_messages)) + tool_definition_tokens(
            tools,
            deferred_tool_names,
        )

    return _StaticTokenEstimator(cache(non_prompt_tokens))


def tool_definition_tokens(tools: Sequence[ToolSpec], deferred_tool_names: frozenset[str]) -> int:
    """Estimate each tool's set definition fields as a request carries them; tool search keeps deferred tools out."""
    definitions = [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in tools
        if tool.name not in deferred_tool_names
    ]
    return estimate_text_tokens(stable_serialize(definitions))
