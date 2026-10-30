"""Tests for mem0-specific memory behavior."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import json
import weakref
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import httpx
import pytest
from anthropic.types import Message as AnthropicMessage
from mem0 import AsyncMemory
from mem0.llms.openai import OpenAILLM
from ollama import ChatResponse as OllamaChatResponse
from ollama import Message as OllamaMessage
from openai import AuthenticationError, InternalServerError
from openai.types.chat import ChatCompletion
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.memory import _MemoryEmbedderConfig, _MemoryLLMConfig
from mindroom.config.models import EmbedderConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder, get_embedder_failure
from mindroom.embeddings.errors import EmbedderRequestError, embedder_failure_is_transient, openai_embedder_error
from mindroom.helper_usage import HelperUsageOwner, helper_usage_owner_context
from mindroom.llm.messages import ModelProvenance
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.memory import (
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    redact_memory_text,
    search_agent_memories,
    store_conversation_memory,
    update_agent_memory,
)
from mindroom.memory._mem0_backend import Mem0MemoryBackend
from mindroom.memory._mem0_usage import counted_mem0_usage, install_mem0_usage_capture, mem0_usage
from mindroom.memory._shared import MemoryNotFoundError
from mindroom.memory.config import _Mem0Embedder, _Mem0StrictOpenAIEmbedder, mem0_chroma_path
from mindroom.redaction import REDACTED, REDACTION_FAILED
from mindroom.tool_system.runtime_context import detached_tool_scope
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    private_instance_scope_root_path,
    resolve_worker_key,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig, fake_mem0_stores

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.llm.usage import HelperUsage


async def _search_memory_results(*args: object, **kwargs: object) -> list:
    outcome = await search_agent_memories(*args, **kwargs)
    assert outcome.degraded_reason is None
    return outcome.results


@pytest.fixture
def storage_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(storage_path: Path) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=storage_path / "config.yaml",
        storage_path=storage_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    return bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", role="General assistant"),
                "calculator": AgentConfig(display_name="Calculator", role="Calculator assistant"),
            },
        ),
        runtime_paths,
    )


@pytest.mark.asyncio
async def test_store_conversation_memory_uses_explicit_execution_identity_for_deferred_mem0_writes(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user"

    stores, create_fake_memory_instance = fake_mem0_stores()

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_fake_memory_instance):
        await store_conversation_memory(
            "Alice-authored shared memory",
            "general",
            "session-alice",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )

    general_root = agent_state_root_path(storage_path, "general")
    assert list(stores) == [general_root]
    assert [(entry["user_id"], entry["metadata"]) for entry in (await stores[general_root].get_all())["results"]] == [
        ("agent_general", {"type": "conversation", "session_id": "session-alice", "agent": "general"}),
    ]


@pytest.mark.asyncio
async def test_store_conversation_memory_redacts_embedder_provider_error(
    config: Config,
) -> None:
    """Automatic Mem0 writes never log the rejected key from a provider body."""
    config.memory.backend = "mem0"
    secret = "sk-secret-automatic-store"  # noqa: S105
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(401, request=request, json={"error": {"message": f"rejected {secret}"}})
    error = AuthenticationError(f"rejected {secret}", response=response, body=None)

    class FailingMemory:
        async def add(self, *_args: object, **_kwargs: object) -> None:
            # Mem0's embedder is MindRoom's, which raises the classified error at its boundary.
            raise openai_embedder_error(error) from error

    try:
        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=FailingMemory()),
            capture_logs() as logs,
        ):
            await store_conversation_memory(
                "Remember this",
                "general",
                "session",
                config,
                runtime_paths_for(config),
            )

        assert secret not in str(logs)
        assert "embedder authentication failed (HTTP 401)" in str(logs)
        assert "Failed to add memory" in str(logs)
        assert "Memory added" not in str(logs)
    finally:
        capture_embedder_health_recorder().record(None)


@pytest.mark.asyncio
async def test_store_conversation_memory_detects_embedding_failure_swallowed_by_mem0(
    config: Config,
) -> None:
    """Automatic writes remain degraded when Mem0 catches every embedding error."""
    config.memory.backend = "mem0"
    failure_detail = "embedder authentication failed (HTTP 401)"

    class FailingEmbedder:
        sends_batches = True

        def embed(self, _texts: list[str], *, purpose: str) -> list[list[float]]:
            del purpose
            raise EmbedderRequestError(failure_detail)

        async def aembed(self, _texts: list[str], *, purpose: str) -> list[list[float]]:
            del purpose
            raise EmbedderRequestError(failure_detail)

    class SwallowingMemory:
        def __init__(self) -> None:
            self.embedding_model = _Mem0Embedder(_Mem0StrictOpenAIEmbedder(FailingEmbedder()), openai_embedder_error)

        async def add(self, messages: list[dict], **_kwargs: object) -> dict[str, list]:
            try:
                self.embedding_model.embed_batch([message["content"] for message in messages])
            except EmbedderRequestError:
                return {"results": []}
            return {"results": []}

    try:
        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=SwallowingMemory()),
            capture_logs() as logs,
        ):
            await store_conversation_memory(
                "Remember this",
                "general",
                "session",
                config,
                runtime_paths_for(config),
            )

        assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        assert "embedder authentication failed (HTTP 401)" in str(logs)
        assert "Failed to add memory" in str(logs)
        assert "Memory added" not in str(logs)
    finally:
        capture_embedder_health_recorder().record(None)


class _OllamaServerThatFailsAfterExtraction:
    """Answer Mem0's own Ollama clients offline; once the memory model has extracted its facts, the server is down."""

    def __init__(self, facts: list[str]) -> None:
        self.facts = facts
        self.extracted = False

    def client(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(list=self.list_models, embed=self.embed, chat=self.chat)

    def list_models(self) -> dict[str, list[dict[str, str]]]:
        return {"models": [{"model": "nomic-embed-text:latest"}]}

    def embed(self, *, model: str, input: str | list[str]) -> dict[str, list[list[float]]]:  # noqa: A002 - Ollama's name
        del model
        if self.extracted:
            msg = "Failed to connect to Ollama. Please check that Ollama is downloaded and running."
            raise ConnectionError(msg)
        return {"embeddings": [[1.0, 0.0, 0.0, 0.0]] * (len(input) if isinstance(input, list) else 1)}

    def chat(self, **_request: object) -> OllamaChatResponse:
        self.extracted = True
        content = json.dumps({"memory": [{"text": fact} for fact in self.facts]})
        return OllamaChatResponse(model="extractor", message=OllamaMessage(role="assistant", content=content))


@pytest.mark.asyncio
async def test_a_memory_whose_ollama_embedding_fails_after_extraction_is_not_saved_silently(
    config: Config,
) -> None:
    """Mem0 drops the extracted facts it cannot embed and reports the add as done, so the save raises the failure."""
    config.memory.backend = "mem0"
    config.memory.embedder = _MemoryEmbedderConfig(provider="ollama", config=EmbedderConfig(model="nomic-embed-text"))
    config.memory.llm = _MemoryLLMConfig(provider="ollama", config={"model": "extractor"})
    server = _OllamaServerThatFailsAfterExtraction(["Atlas ships Friday"])
    runtime_paths = runtime_paths_for(config)

    try:
        with (
            patch("mem0.embeddings.ollama.Client", server.client),
            patch("mem0.llms.ollama.Client", server.client),
            pytest.raises(EmbedderRequestError) as excinfo,
        ):
            await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths)
        assert get_embedder_failure() == "embedder endpoint unreachable"
    finally:
        capture_embedder_health_recorder().record(None)

    assert (str(excinfo.value), excinfo.value.retryable) == ("embedder endpoint unreachable", True)
    with patch("mem0.embeddings.ollama.Client", server.client), patch("mem0.llms.ollama.Client", server.client):
        assert await list_all_agent_memories("general", config, runtime_paths) == []


@pytest.mark.asyncio
async def test_a_failure_of_mem0_s_memory_model_is_not_an_embedder_failure(config: Config) -> None:
    """A rejected memory LLM key propagates as the model's error and leaves the embedder's health alone."""
    config.memory.backend = "mem0"
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(401, request=request, json={"error": {"message": "Incorrect API key provided"}})
    rejected = AuthenticationError("Incorrect API key provided", response=response, body=None)

    class FailingMemory:
        async def add(self, *_args: object, **_kwargs: object) -> None:
            # Mem0's own model calls raise the raw SDK error.
            raise rejected

    capture_embedder_health_recorder().record(None)
    with (
        patch("mindroom.memory._backend.create_memory_instance", return_value=FailingMemory()),
        pytest.raises(AuthenticationError),
    ):
        await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths_for(config))

    assert get_embedder_failure() is None


@pytest.mark.asyncio
async def test_mem0_write_failure_keeps_the_embedder_retry_judgment(config: Config) -> None:
    """A failed Mem0 write raises the classified error with its retry flag and hint, and without the raw error."""
    config.memory.backend = "mem0"
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(503, request=request, headers={"retry-after": "3"}, json={"error": {"message": "busy"}})
    raw_error = InternalServerError("busy", response=response, body=None)

    class FailingMemory:
        async def add(self, *_args: object, **_kwargs: object) -> None:
            raise openai_embedder_error(raw_error) from raw_error

    try:
        with (
            patch("mindroom.memory._backend.create_memory_instance", return_value=FailingMemory()),
            pytest.raises(EmbedderRequestError) as excinfo,
        ):
            await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths_for(config))
    finally:
        capture_embedder_health_recorder().record(None)

    assert str(excinfo.value) == "embedder request failed (HTTP 503)"
    assert embedder_failure_is_transient(excinfo.value)
    assert excinfo.value.retry_after_seconds == 3.0
    assert excinfo.value.__cause__ is None


@pytest.mark.asyncio
async def test_private_agent_explicit_mem0_uses_private_instance_storage(
    storage_path: Path,
    config: Config,
) -> None:
    """Explicit mem0 CRUD for private agents should stay inside the private-instance root."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")

    memories_by_path, create_fake_memory_instance = fake_mem0_stores()

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    worker_key = resolve_worker_key("user", execution_identity, agent_name="general")

    assert worker_key is not None

    with (
        patch("mindroom.memory._backend.create_memory_instance", side_effect=create_fake_memory_instance),
        detached_tool_scope(execution_identity),
    ):
        await add_agent_memory(
            "Private note",
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        search_results = await _search_memory_results(
            "Private note",
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
            limit=10,
        )
        listed = await list_all_agent_memories(
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
            limit=10,
        )

        assert len(listed) == 1
        memory_id = listed[0]["id"]
        loaded = await get_agent_memory(
            memory_id,
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert loaded is not None

        await update_agent_memory(
            memory_id,
            "Updated private note",
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        updated = await get_agent_memory(
            memory_id,
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert updated is not None
        assert updated["memory"] == "Updated private note"

        await delete_agent_memory(
            memory_id,
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        deleted = await get_agent_memory(
            memory_id,
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert deleted is None

    expected_private_path = private_instance_scope_root_path(storage_path, worker_key) / "general"
    assert set(memories_by_path) == {expected_private_path}
    assert agent_state_root_path(storage_path, "general") not in memories_by_path
    assert any(result.get("memory") == "Private note" for result in search_results)


async def _scope_entries(store: FakeMem0ScopedMemory, scope_user_id: str) -> list[str]:
    return [str(entry["memory"]) for entry in (await store.get_all(filters={"user_id": scope_user_id}))["results"]]


_TEAM_ID = "team_calculator+general"


@pytest.mark.asyncio
async def test_mem0_team_conversation_memory_is_one_store_every_requester_and_member_reads(
    storage_path: Path,
    config: Config,
) -> None:
    """A team turn is extracted once into the team's own store, which user-scoped workers do not partition."""
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user"
    config.agents["calculator"].worker_scope = "user"
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="team",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="team",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )
    team_root = storage_path / "team_memory"
    stores, create_memory = fake_mem0_stores()

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_memory):
        with detached_tool_scope(alice_identity):
            await store_conversation_memory(
                "Alice-authored shared team memory",
                ["general", "calculator"],
                "session-alice",
                config,
                runtime_paths_for(config),
            )
            assert list(stores) == [team_root]
            alice_results = await _search_memory_results(
                "Alice-authored shared team",
                "general",
                config,
                runtime_paths_for(config),
                limit=5,
            )

        with detached_tool_scope(bob_identity):
            bob_results = await _search_memory_results(
                "Alice-authored shared team",
                "calculator",
                config,
                runtime_paths_for(config),
                limit=5,
            )

    [alice_hit] = alice_results
    [bob_hit] = bob_results
    assert alice_hit["memory"] == "Alice-authored shared team memory"
    assert bob_hit["id"] == alice_hit["id"]
    team_entries = {root: await _scope_entries(store, _TEAM_ID) for root, store in stores.items()}
    assert {root: entries for root, entries in team_entries.items() if entries} == {
        team_root: ["Alice-authored shared team memory"],
    }


@pytest.mark.asyncio
async def test_mixed_private_team_mem0_member_crud_is_rejected(storage_path: Path, config: Config) -> None:
    """A member of a configured team with a private member can add and list, but not read across its team scopes."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}
    stores, create_memory = fake_mem0_stores()
    rejection = "private agents are only supported in explicit Matrix ad hoc teams with requester identity"

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_memory):
        await add_agent_memory("Shared calculator note", "calculator", config, runtime_paths_for(config))
        calculator_memory_id = (await list_all_agent_memories("calculator", config, runtime_paths_for(config)))[0]["id"]

        with pytest.raises(ValueError, match=rejection):
            await get_agent_memory(calculator_memory_id, "calculator", config, runtime_paths_for(config))
        with pytest.raises(ValueError, match=rejection):
            await update_agent_memory(
                calculator_memory_id,
                "Updated shared calculator note",
                "calculator",
                config,
                runtime_paths_for(config),
            )
        with pytest.raises(ValueError, match=rejection):
            await delete_agent_memory(calculator_memory_id, "calculator", config, runtime_paths_for(config))

    assert list(stores) == [agent_state_root_path(storage_path, "calculator")]


@pytest.mark.asyncio
async def test_reads_never_create_a_team_store_no_turn_has_written(storage_path: Path, config: Config) -> None:
    """Reading before a team's first turn neither opens nor creates the team's store."""
    config.memory.backend = "mem0"
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    runtime_paths = runtime_paths_for(config)
    stores, create_memory = fake_mem0_stores()

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_memory):
        await add_agent_memory("General note", "general", config, runtime_paths)
        [hit] = await _search_memory_results("General note", "general", config, runtime_paths, limit=5)
        assert await get_agent_memory("unknown-id", "general", config, runtime_paths) is None
        with pytest.raises(MemoryNotFoundError):
            await update_agent_memory("unknown-id", "Edited", "general", config, runtime_paths)
        with pytest.raises(MemoryNotFoundError):
            await delete_agent_memory("unknown-id", "general", config, runtime_paths)

    assert hit["memory"] == "General note"
    assert list(stores) == [agent_state_root_path(storage_path, "general")]
    assert not (storage_path / "team_memory").exists()


@pytest.mark.asyncio
async def test_a_team_memory_is_one_record_every_member_edits_alone(storage_path: Path, config: Config) -> None:
    """An edit or deletion through either member changes only that team memory, whichever worker roots they use."""
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}
    runtime_paths = runtime_paths_for(config)
    team_root = storage_path / "team_memory"
    stores, create_memory = fake_mem0_stores()

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:example.org:$thread",
    )

    with (
        patch("mindroom.memory._backend.create_memory_instance", side_effect=create_memory),
        detached_tool_scope(execution_identity),
    ):
        for note in ("Team shared note", "Team launch plan"):
            await store_conversation_memory(note, ["general", "calculator"], "session-alice", config, runtime_paths)
        assert list(stores) == [team_root]

        [general_hit] = await _search_memory_results("shared note", "general", config, runtime_paths, limit=10)
        [calculator_hit] = await _search_memory_results("shared note", "calculator", config, runtime_paths, limit=10)
        memory_id = general_hit["id"]
        assert calculator_hit["id"] == memory_id
        loaded = await get_agent_memory(memory_id, "calculator", config, runtime_paths)
        assert loaded is not None
        assert loaded["memory"] == "Team shared note"

        await update_agent_memory(memory_id, "Updated team shared note", "calculator", config, runtime_paths)
        updated = await get_agent_memory(memory_id, "general", config, runtime_paths)
        assert updated is not None
        assert updated["memory"] == "Updated team shared note"
        assert await _scope_entries(stores[team_root], _TEAM_ID) == ["Updated team shared note", "Team launch plan"]

        await delete_agent_memory(memory_id, "general", config, runtime_paths)
        assert await get_agent_memory(memory_id, "calculator", config, runtime_paths) is None
        assert await _scope_entries(stores[team_root], _TEAM_ID) == ["Team launch plan"]

    assert set(stores) == {
        team_root,
        agent_state_root_path(storage_path, "general"),
        agent_state_root_path(storage_path, "calculator"),
    }


def _chat_completion(prompt_tokens: int, completion_tokens: int, **usage: object) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "completion",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6-luna",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                **usage,
            },
        },
    )


def _mem0_memory_reporting_usage(
    config: Config,
    *,
    fail_after_extraction: bool = False,
) -> Callable[..., SimpleNamespace]:
    """Return a Mem0 factory whose memories run one extraction call reporting 120 input and 30 output tokens per add."""
    config.memory.backend = "mem0"
    config.memory.llm = _MemoryLLMConfig(provider="openai", config={"api_key": "test-key", "model": "gpt-6-luna"})

    def from_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock(return_value=_chat_completion(120, 30)))),
        )
        with patch("mem0.llms.openai.OpenAI", return_value=client):
            mem0_llm = OpenAILLM(config.memory.llm.config)

        async def add(messages: list[dict], **_: object) -> None:
            # Mem0 runs its extraction prompt in a worker thread.
            await asyncio.to_thread(mem0_llm.generate_response, messages=messages)
            if fail_after_extraction:
                msg = "the vector store rejected the extracted facts"
                raise RuntimeError(msg)

        return SimpleNamespace(llm=mem0_llm, add=add, vector_store=MagicMock())

    return from_config


def _recorder() -> tuple[list[tuple[HelperUsage, dict[str, object]]], Callable[..., Awaitable[None]]]:
    recorded: list[tuple[HelperUsage, dict[str, object]]] = []

    async def record(usage: HelperUsage, **kwargs: object) -> None:
        recorded.append((usage, kwargs))

    return recorded, record


@pytest.mark.asyncio
async def test_conversation_memory_extraction_records_its_token_usage(config: Config) -> None:
    """Mem0 calls its own LLM client, so its extraction tokens are recorded for the conversation that produced them."""
    recorded, record = _recorder()

    with (
        patch.object(AsyncMemory, "from_config", side_effect=_mem0_memory_reporting_usage(config)),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=record),
    ):
        await store_conversation_memory(
            "Remember that Atlas ships Friday.",
            "general",
            "session-alice",
            config,
            runtime_paths_for(config),
            user_id="@alice:example.org",
        )

    [(run, kwargs)] = recorded
    owner = cast("HelperUsageOwner", kwargs["owner"])
    assert (owner.session_id, kwargs["kind"], kwargs["requester_id"]) == (
        "session-alice",
        "memory_extraction",
        "@alice:example.org",
    )
    assert (run.metrics.tokens.input_tokens, run.metrics.tokens.output_tokens, run.metrics.tokens.total_tokens) == (
        120,
        30,
        150,
    )
    # Each extraction call is one request in usage reports.
    [request] = run.requests
    assert request.metrics.tokens.total_tokens == 150
    assert request.model == ModelProvenance(model_id="gpt-6-luna", provider="OpenAI Chat")


@pytest.mark.asyncio
async def test_team_memory_extraction_counts_as_internal_usage(config: Config) -> None:
    """A team conversation has no single agent session, so its extraction is reported as internal work."""
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}
    fake_stores = MagicMock(side_effect=_mem0_memory_reporting_usage(config))
    recorded, record = _recorder()

    with (
        patch.object(AsyncMemory, "from_config", fake_stores),
        patch("mindroom.memory._mem0_backend.record_system_usage", side_effect=record),
    ):
        await store_conversation_memory(
            "Remember that Atlas ships Friday.",
            ["general", "calculator"],
            "team-session",
            config,
            runtime_paths_for(config),
            user_id="@alice:example.org",
        )

    [(run, kwargs)] = recorded
    # Internal work names no requester, like the team's routing and mode decisions.
    assert kwargs == {"runtime_paths": runtime_paths_for(config), "kind": "memory_extraction"}
    # A team turn is extracted once, into the team's one store.
    assert fake_stores.call_count == 1
    assert run.metrics.tokens.total_tokens == 150
    assert len(run.requests) == 1


@pytest.mark.asyncio
async def test_team_extraction_usage_is_recorded_when_storing_the_facts_fails(config: Config) -> None:
    """A team turn's extraction is paid for even when the team's store rejects the extracted facts."""
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}
    recorded, record = _recorder()

    with (
        patch.object(
            AsyncMemory,
            "from_config",
            side_effect=_mem0_memory_reporting_usage(config, fail_after_extraction=True),
        ),
        patch("mindroom.memory._mem0_backend.record_system_usage", side_effect=record),
        capture_logs() as logs,
    ):
        await store_conversation_memory(
            "Remember that Atlas ships Friday.",
            ["general", "calculator"],
            "team-session",
            config,
            runtime_paths_for(config),
        )

    [(run, _kwargs)] = recorded
    assert run.metrics.tokens.total_tokens == 150
    assert any(log["event"] == "Failed to add memory" for log in logs)
    assert not any(log["event"] == "Memory added" for log in logs)


@pytest.mark.asyncio
async def test_memory_tool_additions_record_usage_for_the_reply_that_made_them(
    config: Config,
) -> None:
    """The memory tool runs inside a reply, so its extraction tokens go to that reply's conversation and requester."""
    owner = HelperUsageOwner(storage_factory=MagicMock(), session_id="reply-session")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="reply-session",
    )
    recorded, record = _recorder()

    with (
        patch.object(AsyncMemory, "from_config", side_effect=_mem0_memory_reporting_usage(config)),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=record),
        helper_usage_owner_context(owner),
    ):
        await add_agent_memory(
            "Atlas ships Friday.",
            "general",
            config,
            runtime_paths_for(config),
            execution_identity=identity,
        )

    [(run, kwargs)] = recorded
    assert (kwargs["owner"], kwargs["kind"], kwargs["requester_id"]) == (
        owner,
        "memory_extraction",
        "@alice:example.org",
    )
    assert run.metrics.tokens.total_tokens == 150


@pytest.mark.asyncio
async def test_memory_addition_outside_a_reply_counts_as_internal_usage(config: Config) -> None:
    """An addition with no reply or call to charge is reported as internal work."""
    recorded, record = _recorder()

    with (
        patch.object(AsyncMemory, "from_config", side_effect=_mem0_memory_reporting_usage(config)),
        patch("mindroom.memory._mem0_backend.record_system_usage", side_effect=record),
    ):
        await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths_for(config))

    [(run, kwargs)] = recorded
    assert kwargs["kind"] == "memory_extraction"
    assert run.metrics.tokens.total_tokens == 150


@pytest.mark.asyncio
async def test_memory_addition_records_usage_when_storing_the_facts_fails(config: Config) -> None:
    """The extraction call is paid for even when storing what it extracted fails afterwards."""
    recorded, record = _recorder()

    with (
        patch.object(
            AsyncMemory,
            "from_config",
            side_effect=_mem0_memory_reporting_usage(config, fail_after_extraction=True),
        ),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=record),
        helper_usage_owner_context(HelperUsageOwner(storage_factory=MagicMock(), session_id="reply-session")),
        pytest.raises(RuntimeError, match="rejected the extracted facts"),
    ):
        await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths_for(config))

    [(run, _kwargs)] = recorded
    assert run.metrics.tokens.total_tokens == 150


@pytest.mark.asyncio
async def test_memory_addition_succeeds_when_its_usage_cannot_be_recorded(config: Config) -> None:
    """Usage recording comes after the memory is stored, so a failure there does not fail the memory tool."""
    with (
        patch.object(AsyncMemory, "from_config", side_effect=_mem0_memory_reporting_usage(config)),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=OSError("usage store is locked")),
        helper_usage_owner_context(HelperUsageOwner(storage_factory=MagicMock(), session_id="reply-session")),
        capture_logs() as logs,
    ):
        await add_agent_memory("Atlas ships Friday.", "general", config, runtime_paths_for(config))

    assert any(log["event"] == "Memory extraction usage could not be recorded" for log in logs)


@pytest.mark.parametrize(
    ("provider", "client", "expected"),
    [
        pytest.param(
            "openai",
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_: _chat_completion(
                            100,
                            20,
                            prompt_tokens_details={"cached_tokens": 40},
                            completion_tokens_details={"reasoning_tokens": 8},
                        ),
                    ),
                ),
            ),
            (100, 20, 40, 0, 8),
            id="openai",
        ),
        pytest.param(
            "anthropic",
            SimpleNamespace(
                messages=SimpleNamespace(
                    create=lambda **_: AnthropicMessage.model_validate(
                        {
                            "id": "message",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                            "model": "extractor",
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {
                                "input_tokens": 90,
                                "output_tokens": 10,
                                "cache_read_input_tokens": 30,
                                "cache_creation_input_tokens": 20,
                            },
                        },
                    ),
                ),
            ),
            (90, 10, 30, 20, 0),
            id="anthropic",
        ),
        pytest.param(
            "ollama",
            SimpleNamespace(
                chat=lambda **_: OllamaChatResponse(
                    model="extractor",
                    message=OllamaMessage(role="assistant", content="{}"),
                    prompt_eval_count=70,
                    eval_count=5,
                ),
            ),
            (70, 5, 0, 0, 0),
            id="ollama",
        ),
    ],
)
def test_each_provider_reports_its_extraction_tokens(
    provider: str,
    client: SimpleNamespace,
    expected: tuple[int, int, int, int, int],
) -> None:
    """Each provider SDK reports usage in its own shape."""
    llm = SimpleNamespace(config=SimpleNamespace(model="extractor"), client=client)
    install_mem0_usage_capture(llm, provider)
    call = {
        "openai": lambda: llm.client.chat.completions.create(),
        "anthropic": lambda: llm.client.messages.create(),
        "ollama": lambda: llm.client.chat(),
    }[provider]

    call()  # Outside a memory write, nothing is collected.
    with counted_mem0_usage() as calls:
        call()

    usage = mem0_usage(calls)
    assert usage is not None
    metrics = usage.metrics
    assert (
        metrics.tokens.input_tokens,
        metrics.tokens.output_tokens,
        metrics.tokens.cache_read_tokens,
        metrics.tokens.cache_write_tokens,
        metrics.tokens.reasoning_tokens,
    ) == expected


def test_extraction_usage_is_dated_by_its_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call is dated when it answered, and the write's usage when its first call did, not when it is recorded."""
    answered = iter([1_700_000_000.5, 1_700_000_007.5])
    monkeypatch.setattr("mindroom.memory._mem0_usage.time", lambda: next(answered))
    completion = _chat_completion(10, 2)
    llm = SimpleNamespace(
        config=SimpleNamespace(model="extractor"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: completion))),
    )
    install_mem0_usage_capture(llm, "openai")

    with counted_mem0_usage() as calls:
        llm.client.chat.completions.create()
        llm.client.chat.completions.create()

    usage = mem0_usage(calls)
    assert usage is not None
    assert usage.created_at == 1_700_000_000
    assert [request.created_at for request in usage.requests] == [1_700_000_000, 1_700_000_007]


def test_calls_without_reported_usage_record_nothing() -> None:
    """A provider that reports no usage leaves nothing to record rather than a zero-token request."""
    completion = _chat_completion(0, 0).model_copy(update={"usage": None})
    llm = SimpleNamespace(
        config=SimpleNamespace(model="extractor"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: completion))),
    )
    install_mem0_usage_capture(llm, "openai")

    with counted_mem0_usage() as calls:
        llm.client.chat.completions.create()

    assert mem0_usage(calls) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("with_history", [False, True])
async def test_conversation_memory_removes_credentials_before_mem0(
    config: Config,
    with_history: bool,
) -> None:
    """Mem0 extracts and stores memories from what it is given, so credential-like values are removed first."""
    config.memory.backend = "mem0"
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    # A line longer than redaction scans is dropped like an unreadable one.
    oversized = "x" * 70_000
    history = [
        ResolvedVisibleMessage.synthetic(sender="@alice:example.org", body=f"My token is {token}", event_id="$1"),
        ResolvedVisibleMessage.synthetic(sender="@general:example.org", body="Noted, Atlas is yours.", event_id="$2"),
    ]
    sent: list[list[dict]] = []

    class FakeScopedMemory:
        async def add(self, messages: list[dict], **_: object) -> None:
            sent.append(messages)

    with patch("mindroom.memory._backend.create_memory_instance", return_value=FakeScopedMemory()):
        await store_conversation_memory(
            # A line redaction cannot read safely, such as an unclosed quote, is dropped without the rest.
            f'The staging token is {token}.\nenv:\n  password:\nSet the DB password: "correct horse\n{oversized}\n'
            "Use API key: my-secret-key for the local endpoint.\nThe launch is Friday.",
            "general",
            "session-alice",
            config,
            runtime_paths_for(config),
            history if with_history else None,
            "@alice:example.org",
        )

    [messages] = sent
    assert len(messages) == (3 if with_history else 1)
    assert all(token not in message["content"] and "correct horse" not in message["content"] for message in messages)
    assert REDACTION_FAILED not in messages[-1]["content"]
    assert oversized not in messages[-1]["content"]
    # A key a person chose, after an explicit label, is removed too.
    assert "my-secret-key" not in messages[-1]["content"]
    assert "The launch is Friday." in messages[-1]["content"]
    assert "env:" in messages[-1]["content"]
    if with_history:
        assert messages[1]["content"] == "Noted, Atlas is yours."


def test_memory_text_removes_a_pasted_private_key_whole() -> None:
    """A PEM private key spans several lines, so it is removed from its BEGIN line through its END line."""
    body = "\n".join(f"MIIEvQIBADANBgkqhkiG9w0BAQEFAASC{index:032d}" for index in range(4))
    key = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}\n-----END OPENSSH PRIVATE KEY-----"

    redacted = redact_memory_text(f"The mirror uses this key:\n{key}\nRotate it yearly.")

    assert redacted == f"The mirror uses this key:\n{REDACTED}\nRotate it yearly."


@pytest.mark.asyncio
async def test_a_memory_both_scopes_return_appears_once_per_scope_ranked_within_the_limit(
    storage_path: Path,
    config: Config,
) -> None:
    """Agent and team hits rank together by score, and a text both scopes hold is two memories with their own IDs."""
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    mem0_chroma_path(storage_path / "team_memory").mkdir(parents=True)
    hits_by_scope: dict[str, list[dict[str, object]]] = {
        "agent_general": [
            {"id": "agent-shared", "memory": "Shared fact", "score": 0.5},
            {"id": "agent-a", "memory": "Agent fact A", "score": 0.7},
            {"id": "agent-b", "memory": "Agent fact B", "score": 0.2},
        ],
        _TEAM_ID: [
            {"id": "team-shared", "memory": "Shared fact", "score": 0.9},
            {"id": "team-c", "memory": "Team fact C", "score": 0.3},
        ],
    }

    class ScopedSearch:
        async def search(
            self,
            query: str,
            *,
            filters: dict[str, object],
            top_k: int,
            threshold: float,
        ) -> dict[str, list[dict[str, object]]]:
            del query, threshold
            return {"results": hits_by_scope[str(filters["user_id"])][:top_k]}

    async def create_memory(_store_root: Path, _config: Config) -> ScopedSearch:
        return ScopedSearch()

    backend = Mem0MemoryBackend(runtime_paths=runtime_paths_for(config), create_memory=create_memory)

    outcome = await backend.search("shared", "general", config, limit=4)

    assert [(result["id"], result["score"]) for result in outcome.results] == [
        ("team-shared", 0.9),
        ("agent-a", 0.7),
        ("agent-shared", 0.5),
        ("team-c", 0.3),
    ]


@pytest.mark.asyncio
async def test_a_team_store_s_embedder_failure_degrades_search_but_keeps_the_agent_store_s_hits(
    storage_path: Path,
    config: Config,
) -> None:
    """Stores are built and searched one at a time, and a team store's embedder failure keeps the agent store's hits."""
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    mem0_chroma_path(storage_path / "team_memory").mkdir(parents=True)
    failure_detail = "embedder authentication failed (HTTP 401)"
    events: list[str] = []
    agent_hit: dict[str, object] = {"id": "agent-a", "memory": "Agent fact", "score": 0.6}

    class StoreSearch:
        async def search(
            self,
            query: str,
            *,
            filters: dict[str, object],
            top_k: int,
            threshold: float,
        ) -> dict[str, list[dict[str, object]]]:
            del query, top_k, threshold
            scope_user_id = str(filters["user_id"])
            events.append(f"search {scope_user_id}")
            # Yield, so a concurrent build of the next store would run before this search ends.
            await asyncio.sleep(0)
            events.append(f"done {scope_user_id}")
            if scope_user_id == _TEAM_ID:
                raise EmbedderRequestError(failure_detail)
            return {"results": [agent_hit]}

    async def create_memory(store_root: Path, _config: Config) -> StoreSearch:
        events.append(f"build {store_root.name}")
        return StoreSearch()

    backend = Mem0MemoryBackend(runtime_paths=runtime_paths_for(config), create_memory=create_memory)

    try:
        outcome = await backend.search("fact", "general", config, limit=3)
        assert get_embedder_failure() == failure_detail
    finally:
        capture_embedder_health_recorder().record(None)

    assert events == [
        "build general",
        "search agent_general",
        "done agent_general",
        "build team_memory",
        f"search {_TEAM_ID}",
        f"done {_TEAM_ID}",
    ]
    assert outcome.results == [agent_hit]
    assert outcome.degraded_reason == failure_detail


@pytest.mark.asyncio
async def test_a_record_of_another_scope_in_an_agents_store_is_not_that_agents_memory(
    storage_path: Path,
    config: Config,
) -> None:
    """A store answers for its own scope only: a team record an unmerged store still holds is not editable as the agent's."""
    stores, create_memory = fake_mem0_stores()
    backend = Mem0MemoryBackend(runtime_paths=runtime_paths_for(config), create_memory=create_memory)
    agent_store = await create_memory(agent_state_root_path(storage_path, "general"), config)
    await agent_store.add([{"role": "user", "content": "Left team copy"}], user_id=_TEAM_ID, metadata={})
    (left,) = (await agent_store.get_all(filters={"user_id": _TEAM_ID}))["results"]

    assert await backend.get(left["id"], "general", config) is None
    with pytest.raises(MemoryNotFoundError):
        await backend.update(left["id"], "Edited", "general", config)
    assert await _scope_entries(stores[agent_state_root_path(storage_path, "general")], _TEAM_ID) == ["Left team copy"]


@pytest.mark.asyncio
async def test_each_store_is_released_before_the_next_one_is_built(storage_path: Path, config: Config) -> None:
    """Every Mem0 store builds its own embedder, a full copy of a local model, so one search holds one store at a time."""
    config.teams = {"pair": MockTeamConfig(agents=["general", "calculator"])}
    mem0_chroma_path(storage_path / "team_memory").mkdir(parents=True)
    built: list[weakref.ref[FakeMem0ScopedMemory]] = []
    alive_at_build: list[int] = []

    async def create_memory(_store_root: Path, _config: Config) -> FakeMem0ScopedMemory:
        alive_at_build.append(sum(ref() is not None for ref in built))
        memory = FakeMem0ScopedMemory()
        built.append(weakref.ref(memory))
        return memory

    backend = Mem0MemoryBackend(runtime_paths=runtime_paths_for(config), create_memory=create_memory)

    await backend.search("fact", "general", config, limit=3)
    assert await backend.get("unknown-id", "general", config) is None

    assert alive_at_build == [0, 0, 0, 0]
