"""Mem0 memory on a real local Chroma store, with offline embeddings and memory-model replies."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from anthropic.types import Message as AnthropicMessage
from mem0.memory.storage import SQLiteManager

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig, _MemoryLLMConfig
from mindroom.config.models import EmbedderConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.memory import add_agent_memory, list_all_agent_memories, search_agent_memories
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.embeddings.base import EmbeddingPurpose

# The query embeds exactly like the first memory. Chroma's default space is squared L2, and Mem0 scores a hit as
# 1 / (1 + distance), so the memories score 1, 1 / 1.4, and 1 / 3, and the opposite one, at distance 16, falls below
# the 0.1 threshold Mem0 searches with.
_QUERY = "PR review"
_VECTORS = {
    "PR review": [1.0, 0.0, 0.0],
    "Hardware notes": [0.8, 0.6, 0.0],
    "Grocery list": [0.0, 0.0, 1.0],
    "Opposite of everything": [-3.0, 0.0, 0.0],
}
_OTHER_TEXT_VECTOR = [0.0, 1.0, 0.0]


class _TableEmbedder:
    """Embed each known text as its table vector, offline."""

    sends_batches = True

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        del purpose
        return [list(_VECTORS.get(text, _OTHER_TEXT_VECTOR)) for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


def _claude_reply(*, facts: list[str], thinking_first: bool) -> AnthropicMessage:
    """Return a Claude message carrying Mem0's extraction JSON, after a thinking block as thinking models reply."""
    text_block = {"type": "text", "text": json.dumps({"memory": [{"text": fact} for fact in facts]})}
    thinking_block = {"type": "thinking", "thinking": "Listing the facts worth keeping.", "signature": "sig"}
    return AnthropicMessage.model_validate(
        {
            "id": "msg_extraction",
            "type": "message",
            "role": "assistant",
            "content": [thinking_block, text_block] if thinking_first else [text_block],
            "model": "claude-sonnet-5-5",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 50, "output_tokens": 20},
        },
    )


class _FakeAnthropicClient:
    """Answer every Messages API request with one canned reply."""

    def __init__(self, reply: AnthropicMessage) -> None:
        self.messages = self
        self._reply = reply

    def create(self, **_: object) -> AnthropicMessage:
        return self._reply


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a config whose Mem0 memory embeds through OpenAI and extracts with Claude."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    memory = MemoryConfig(
        backend="mem0",
        embedder=_MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(model="text-embedding-3-small", api_key="sk-test"),
        ),
        llm=_MemoryLLMConfig(provider="anthropic", config={"model": "claude-sonnet-5-5", "api_key": "sk-ant-test"}),
    )
    return bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", role="General assistant")}, memory=memory),
        runtime_paths,
    )


@pytest.fixture
def claude_reply() -> list[AnthropicMessage]:
    """Hold the reply the memory model gives; a test replaces it before writing."""
    return [_claude_reply(facts=[], thinking_first=False)]


@pytest.fixture(autouse=True)
def offline_mem0(tmp_path: Path, claude_reply: list[AnthropicMessage]) -> Iterator[None]:
    """Run real Mem0 and Chroma with table embeddings, a canned Claude reply, and history kept under ``tmp_path``."""
    with (
        patch("mindroom.embeddings.factory.create_configured_embedder", return_value=_TableEmbedder()),
        patch("anthropic.Anthropic", side_effect=lambda **_: _FakeAnthropicClient(claude_reply[0])),
        patch("mem0.memory.main.SQLiteManager", side_effect=lambda _path: SQLiteManager(str(tmp_path / "history.db"))),
    ):
        yield


async def _remember(config: Config, claude_reply: list[AnthropicMessage], *, thinking_first: bool) -> None:
    claude_reply[0] = _claude_reply(facts=list(_VECTORS), thinking_first=thinking_first)
    runtime_paths = runtime_paths_for(config)
    await add_agent_memory("Remember my notes.", "general", runtime_paths.storage_root, config, runtime_paths)


@pytest.mark.asyncio
async def test_memory_search_ranks_the_closest_memory_first_and_drops_scores_below_the_threshold(
    config: Config,
    claude_reply: list[AnthropicMessage],
) -> None:
    """Mem0 already reports Chroma hits as similarities, so MindRoom must keep their direction."""
    await _remember(config, claude_reply, thinking_first=False)
    runtime_paths = runtime_paths_for(config)

    outcome = await search_agent_memories(
        _QUERY,
        "general",
        runtime_paths.storage_root,
        config,
        runtime_paths,
        limit=5,
    )

    assert outcome.degraded_reason is None
    assert [result["memory"] for result in outcome.results] == ["PR review", "Hardware notes", "Grocery list"]
    assert [result["score"] for result in outcome.results] == pytest.approx([1.0, 1 / 1.4, 1 / 3])


@pytest.mark.asyncio
async def test_claude_reply_that_starts_with_thinking_stores_its_extracted_memories(
    config: Config,
    claude_reply: list[AnthropicMessage],
) -> None:
    """A Claude extraction reply that opens with a thinking block still stores the facts in its text block."""
    await _remember(config, claude_reply, thinking_first=True)
    runtime_paths = runtime_paths_for(config)

    stored = await list_all_agent_memories("general", runtime_paths.storage_root, config, runtime_paths)

    assert sorted(memory["memory"] for memory in stored) == sorted(_VECTORS)
