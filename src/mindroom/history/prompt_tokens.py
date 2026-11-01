"""Static prompt-token counts: the rendered system messages, the offered tool definitions, and the prompt text.

Compaction consumes these numbers through the resolved execution plan; an agent and a team leader size their
requests through the same estimator.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from mindroom.llm.messages import stable_serialize, system_text
from mindroom.token_counting.counts import count

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import TokenCounter, ToolSpec


@dataclass(frozen=True, slots=True)
class _StaticTokenEstimator:
    """Request-local static-token counts, as one route counts them."""

    counter: TokenCounter
    # The system messages' and tool definitions' tokens, which do not depend on the prompt text.
    non_prompt_tokens: int

    def estimate(self, full_prompt: str) -> int:
        """Count the prompt text's tokens plus the non-prompt cost."""
        return count(self.counter, full_prompt) + self.non_prompt_tokens


def build_static_token_estimator(
    counter: TokenCounter,
    system_messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return the static-token counts of one rendered system prompt and its tools, as ``counter``'s route counts them.

    Deferred tools stay off the request until tool search loads them, so they are left out.
    """
    definitions = tool_definitions_text(tools, deferred_tool_names)
    return _StaticTokenEstimator(counter, count(counter, system_text(system_messages)) + count(counter, definitions))


def tool_definitions_text(tools: Sequence[ToolSpec], deferred_tool_names: frozenset[str]) -> str:
    """Serialize each tool's set definition fields as a request carries them, or "" when it carries none.

    Tool search keeps deferred tools out.
    """
    definitions = [
        {key: value for key, value in asdict(tool).items() if value is not None}
        for tool in tools
        if tool.name not in deferred_tool_names
    ]
    return stable_serialize(definitions) if definitions else ""
