"""Mem0 memory on a real local Chroma store, with offline embeddings and memory-model replies."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from anthropic import AuthenticationError
from anthropic.types import Message as AnthropicMessage
from chromadb.api.rust import RustBindingsAPI
from mem0.embeddings.base import EmbeddingBase
from mem0.utils.factory import EmbedderFactory

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig, _MemoryLLMConfig
from mindroom.config.models import EmbedderConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.memory import MemoryTools
from mindroom.memory import add_agent_memory, list_all_agent_memories, search_agent_memories, store_conversation_memory
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.embeddings.base import EmbeddingPurpose


def _unit(cosine: float, axis: int = 1) -> list[float]:
    """Return a unit vector with ``cosine`` similarity to the query vector, leaning towards ``axis``."""
    vector = [cosine, 0.0, 0.0, 0.0]
    vector[axis] = math.sqrt(1 - cosine**2)
    return vector


# The query embeds as [1, 0, 0, 0]. Chroma measures squared L2 distance, which is 2 - 2 * cosine for unit vectors.
_QUERY = "PR review"
_VECTORS = {
    "PR review": _unit(1.0),
    "Hardware notes": _unit(0.8),
    "Barely related": _unit(0.15),
    "Hardly related": _unit(0.05),
    "Grocery list": _unit(0.0),
    # Not unit length: squared L2 distance 16 from the query.
    "Opposite of everything": [-3.0, 0.0, 0.0, 0.0],
}
# The agent's own memories: three loosely related ones, enough to fill a three-memory recall, and two unrelated ones.
_AGENT_MEMORY_COSINES = {
    "Recipe one": 0.6,
    "Recipe two": 0.5,
    "Recipe three": 0.4,
    "Recipe four": 0.0,
    "Recipe five": 0.0,
}
for _axis, (_text, _cosine) in enumerate(_AGENT_MEMORY_COSINES.items()):
    _VECTORS[_text] = _unit(_cosine, axis=1 + _axis % 3)
_TEAM_MEMORY = "Team PR review checklist"
_VECTORS[_TEAM_MEMORY] = _unit(1.0)
_OTHER_TEXT_VECTOR = _unit(0.0, axis=2)


class _TableEmbedder:
    """Embed each known text as its table vector, offline."""

    sends_batches = True

    def embed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        del purpose
        return [list(_VECTORS.get(text, _OTHER_TEXT_VECTOR)) for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: EmbeddingPurpose) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


class _TableLocalEmbedding(EmbeddingBase):
    """Replace Mem0's huggingface embedder, which both local providers load, with the table vectors, offline."""

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        del memory_action
        return list(_VECTORS.get(text, _OTHER_TEXT_VECTOR))


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
    """Answer every Messages API request with one canned reply, or raise it when it is an error."""

    def __init__(self, reply: AnthropicMessage | Exception, prompts: list[str]) -> None:
        self.messages = self
        self._reply = reply
        self._prompts = prompts

    def create(self, **request: object) -> AnthropicMessage:
        self._prompts.append(json.dumps(request["messages"]))
        if isinstance(self._reply, Exception):
            raise self._reply
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
    agents = {
        "general": AgentConfig(display_name="General", role="General assistant"),
        "calculator": AgentConfig(display_name="Calculator", role="Calculator assistant"),
        "mind": AgentConfig(display_name="Mind", role="Personal assistant", private=AgentPrivateConfig(per="user")),
    }
    teams = {"pair": TeamConfig(display_name="Pair", role="Pair of agents", agents=["general", "calculator"])}
    return bind_runtime_paths(Config(agents=agents, teams=teams, memory=memory), runtime_paths)


@pytest.fixture
def claude_reply() -> list[AnthropicMessage | Exception]:
    """Hold the reply the memory model gives; a test replaces it before writing."""
    return [_claude_reply(facts=[], thinking_first=False)]


@pytest.fixture
def extraction_prompts() -> list[str]:
    """Collect the messages of every request the memory model receives."""
    return []


@pytest.fixture(autouse=True)
def offline_mem0(claude_reply: list[AnthropicMessage | Exception], extraction_prompts: list[str]) -> Iterator[None]:
    """Run real Mem0 and Chroma with table embeddings and a canned Claude reply."""
    with (
        patch("mindroom.embeddings.factory.create_configured_embedder", return_value=_TableEmbedder()),
        patch(
            "anthropic.Anthropic",
            side_effect=lambda **_: _FakeAnthropicClient(claude_reply[0], extraction_prompts),
        ),
    ):
        yield


async def _remember(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
    facts: list[str],
    *,
    thinking_first: bool = False,
) -> None:
    claude_reply[0] = _claude_reply(facts=facts, thinking_first=thinking_first)
    runtime_paths = runtime_paths_for(config)
    await add_agent_memory("Remember my notes.", "general", runtime_paths.storage_root, config, runtime_paths)


async def _search(config: Config, *, limit: int = 5) -> list[tuple[str, float]]:
    runtime_paths = runtime_paths_for(config)
    outcome = await search_agent_memories(
        _QUERY,
        "general",
        runtime_paths.storage_root,
        config,
        runtime_paths,
        limit=limit,
    )
    assert outcome.degraded_reason is None
    return [(result["memory"], result["score"]) for result in outcome.results]


@pytest.mark.asyncio
async def test_memory_search_ranks_the_closest_memory_first_and_drops_cosine_below_one_tenth(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
) -> None:
    """Mem0 reports Chroma hits as 1 / (1 + distance); search keeps their order and the cosine-0.1 cutoff."""
    await _remember(config, claude_reply, ["PR review", "Hardware notes", "Barely related", "Hardly related"])
    await _remember(config, claude_reply, ["Grocery list", "Opposite of everything"])

    results = await _search(config)

    assert [memory for memory, _ in results] == ["PR review", "Hardware notes", "Barely related"]
    assert [score for _, score in results] == pytest.approx([1.0, 1 / 1.4, 1 / 2.7])


@pytest.mark.asyncio
async def test_an_exact_team_memory_outranks_the_agents_loosely_related_memories(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
) -> None:
    """Agent and team hits are ranked together, so the agent's own memories cannot push out a better team match."""
    await _remember(config, claude_reply, list(_AGENT_MEMORY_COSINES))
    claude_reply[0] = _claude_reply(facts=[_TEAM_MEMORY], thinking_first=False)
    runtime_paths = runtime_paths_for(config)
    await store_conversation_memory(
        "Our checklist for PR review.",
        ["general", "calculator"],
        runtime_paths.storage_root,
        "session-pair",
        config,
        runtime_paths,
    )

    # Automatic recall searches with a limit of three, which the agent's own memories could fill.
    recall = await _search(config, limit=3)
    # A larger limit leaves room, so the unrelated agent memories must stay out by the cutoff alone.
    search = await _search(config, limit=5)

    assert [memory for memory, _ in recall] == [_TEAM_MEMORY, "Recipe one", "Recipe two"]
    assert [score for _, score in recall] == pytest.approx([1.0, 1 / 1.8, 1 / 2.0])
    assert [memory for memory, _ in search] == [_TEAM_MEMORY, "Recipe one", "Recipe two", "Recipe three"]


@pytest.mark.asyncio
async def test_claude_reply_that_starts_with_thinking_stores_its_extracted_memories(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
) -> None:
    """A Claude extraction reply that opens with a thinking block still stores the facts in its text block."""
    facts = ["PR review", "Hardware notes", "Grocery list"]
    await _remember(config, claude_reply, facts, thinking_first=True)
    runtime_paths = runtime_paths_for(config)

    stored = await list_all_agent_memories("general", runtime_paths.storage_root, config, runtime_paths)

    assert sorted(memory["memory"] for memory in stored) == sorted(facts)


@pytest.mark.asyncio
async def test_a_failed_memory_model_call_reaches_the_memory_tool_without_the_rejected_key(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
) -> None:
    """Mem0 raises the provider's error text when extraction fails, and the tool output redacts its credentials."""
    rejected_key = "sk-ant-" + "api03-" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8Ba7"
    message = f"Error code: 401 - invalid x-api-key {rejected_key}"
    response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    claude_reply[0] = AuthenticationError(message, response=response, body=None)
    runtime_paths = runtime_paths_for(config)
    tools = MemoryTools("general", runtime_paths.storage_root, config, runtime_paths)

    result = await tools.add_memory("Remember my notes.")

    assert result.startswith("Failed to store memory: LLM extraction failed: Error code: 401 - invalid x-api-key ")
    assert rejected_key not in result


@pytest.mark.asyncio
async def test_a_save_and_a_search_that_first_open_one_store_together_both_succeed(
    config: Config,
    claude_reply: list[AnthropicMessage | Exception],
) -> None:
    """After a restart, the last turn's memory save and the next message's search can first open one store together."""
    config.memory.embedder.provider = "sentence_transformers"
    first_start = threading.Event()
    finish_first_start = threading.Event()
    start_store = RustBindingsAPI.start

    def slow_first_start(api: RustBindingsAPI) -> None:
        # Chroma has registered the store's client and is starting it, which is when an overlapping build fails.
        if not first_start.is_set():
            first_start.set()
            finish_first_start.wait(5)
        start_store(api)

    with (
        patch.dict(EmbedderFactory.provider_to_class, {"huggingface": f"{__name__}.{_TableLocalEmbedding.__name__}"}),
        patch("mindroom.memory.config.ensure_sentence_transformers_dependencies"),
        patch.object(RustBindingsAPI, "start", slow_first_start),
    ):
        saving = asyncio.create_task(_remember(config, claude_reply, ["PR review"]))
        assert await asyncio.to_thread(first_start.wait, 5)
        searching = asyncio.create_task(_search(config))
        # An overlapping build reaches the store during this wait and fails both operations.
        await asyncio.wait({searching}, timeout=0.5)
        finish_first_start.set()
        await saving
        await searching

        assert [memory for memory, _ in await _search(config)] == ["PR review"]


def _requester(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=f"session-{requester_id}",
    )


@pytest.mark.asyncio
async def test_one_requesters_messages_never_reach_another_requesters_private_memory_extraction(
    config: Config,
    extraction_prompts: list[str],
) -> None:
    """Each private instance keeps Mem0's recent-message context beside its own memories, not in one shared file."""
    runtime_paths = runtime_paths_for(config)
    alice = _requester("@alice:example.org")
    bob = _requester("@bob:example.org")
    for identity, prompt in ((alice, "My locker code is 4512."), (bob, "Plan my week.")):
        await store_conversation_memory(
            prompt,
            "mind",
            runtime_paths.storage_root,
            identity.session_id,
            config,
            runtime_paths,
            execution_identity=identity,
        )

    alice_extraction, bob_extraction = extraction_prompts
    assert "4512" in alice_extraction
    assert "Plan my week." in bob_extraction
    assert "4512" not in bob_extraction
    assert set(runtime_paths.storage_root.rglob("history.db")) == {
        resolve_agent_storage("mind", config, runtime_paths, identity).state_root / "mem0" / "history.db"
        for identity in (alice, bob)
    }
