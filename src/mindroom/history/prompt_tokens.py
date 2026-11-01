"""Static prompt-token counts: the rendered system messages, the offered tool definitions, and the prompt text.

Compaction consumes these numbers through the resolved execution plan; an agent and a team leader size their
requests through the same estimator. Each system message and the tool definitions count as separate parts, because
they change at different times and a counting endpoint's exact count of each is cached on its own.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from mindroom.llm.messages import stable_serialize, system_texts
from mindroom.token_counting.counts import count, measure

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import TokenCounter, ToolSpec


@dataclass(frozen=True)
class _StaticTokenEstimator:
    """Request-local static-token counts, as one route counts them."""

    counter: TokenCounter
    # The system messages' and tool definitions' tokens, which do not depend on the prompt text.
    non_prompt_tokens: int

    def estimate(self, full_prompt: str) -> int:
        """Count the prompt text's tokens plus the non-prompt cost."""
        return count(self.counter, full_prompt).tokens + self.non_prompt_tokens


async def build_static_token_estimator(
    counter: TokenCounter,
    system_messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    deferred_tool_names: frozenset[str],
) -> _StaticTokenEstimator:
    """Return the static-token counts of one rendered system prompt and its tools, as ``counter``'s route counts them.

    They are stable inputs, so the reply waits for the provider to count each part it has not counted yet. Deferred
    tools stay off the request until tool search loads them, so they are left out.
    """
    definitions = tool_definitions_text(tools, deferred_tool_names)
    parts = (*system_texts(system_messages), *((definitions,) if definitions else ()))
    await measure(counter, *parts)
    # Encoding a long prompt would stall the replies sharing the event loop.
    return _StaticTokenEstimator(counter, await asyncio.to_thread(_parts_tokens, counter, parts))


def _parts_tokens(counter: TokenCounter, parts: Sequence[str]) -> int:
    return sum(count(counter, part).tokens for part in parts)


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
