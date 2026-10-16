"""Tests for mem0-specific memory behavior."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import httpx
import pytest
from mem0 import AsyncMemory
from mem0.llms.openai import OpenAILLM
from openai import AuthenticationError
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.memory import _MemoryLLMConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder, get_embedder_failure
from mindroom.embeddings.errors import EmbedderRequestError
from mindroom.helper_usage import HelperUsageOwner, helper_usage_owner_context
from mindroom.memory import (
    add_agent_memory,
    delete_agent_memory,
    get_agent_memory,
    list_all_agent_memories,
    search_agent_memories,
    store_conversation_memory,
    update_agent_memory,
)
from mindroom.memory._mem0_usage import counted_mem0_usage, install_mem0_usage_capture, mem0_usage_run
from mindroom.memory.config import _Mem0StrictOpenAIEmbedder
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    private_instance_scope_root_path,
    resolve_worker_key,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for
from tests.memory_test_support import FakeMem0ScopedMemory, MockTeamConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.run.agent import RunOutput


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

    captured_calls: list[tuple[Path, str | None, dict[str, object]]] = []

    class FakeScopedMemory:
        def __init__(self, scope_storage_path: Path) -> None:
            self.scope_storage_path = scope_storage_path

        async def add(
            self,
            messages: list[dict],
            *,
            user_id: str | None = None,
            metadata: dict[str, object] | None = None,
        ) -> None:
            del messages
            captured_calls.append((self.scope_storage_path, user_id, metadata or {}))

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeScopedMemory:
        del runtime_paths, timing_scope
        return FakeScopedMemory(scope_storage_path)

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
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )

    expected_storage_path = agent_state_root_path(storage_path, "general")
    assert captured_calls == [
        (
            expected_storage_path,
            "agent_general",
            {"type": "conversation", "session_id": "session-alice", "agent": "general"},
        ),
    ]


@pytest.mark.asyncio
async def test_store_conversation_memory_redacts_embedder_provider_error(
    storage_path: Path,
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
            raise error

    async def create_failing_memory(*_args: object, **_kwargs: object) -> FailingMemory:
        return FailingMemory()

    try:
        with (
            patch("mindroom.memory._backend.create_memory_instance", side_effect=create_failing_memory),
            capture_logs() as logs,
        ):
            await store_conversation_memory(
                "Remember this",
                "general",
                storage_path,
                "session",
                config,
                runtime_paths_for(config),
            )

        assert secret not in str(logs)
        assert "embedder authentication failed (HTTP 401)" in str(logs)
        assert "Conversation memory was not stored in any target" in str(logs)
        assert "Memory added" not in str(logs)
    finally:
        capture_embedder_health_recorder().record(None)


@pytest.mark.asyncio
async def test_store_conversation_memory_detects_embedding_failure_swallowed_by_mem0(
    storage_path: Path,
    config: Config,
) -> None:
    """Automatic writes remain degraded when Mem0 catches every embedding error."""
    config.memory.backend = "mem0"
    failure_detail = "embedder authentication failed (HTTP 401)"

    class FailingEmbedder:
        def get_embedding(self, _text: str) -> list[float]:
            raise EmbedderRequestError(failure_detail)

        def get_embeddings_batch(self, _texts: list[str]) -> list[list[float]]:
            raise EmbedderRequestError(failure_detail)

    class SwallowingMemory:
        def __init__(self) -> None:
            self.embedding_model = _Mem0StrictOpenAIEmbedder(FailingEmbedder())

        async def add(self, messages: list[dict], **_kwargs: object) -> dict[str, list]:
            try:
                self.embedding_model.embed_batch([message["content"] for message in messages])
            except EmbedderRequestError:
                return {"results": []}
            return {"results": []}

    async def create_swallowing_memory(*_args: object, **_kwargs: object) -> SwallowingMemory:
        return SwallowingMemory()

    try:
        with (
            patch("mindroom.memory._backend.create_memory_instance", side_effect=create_swallowing_memory),
            capture_logs() as logs,
        ):
            await store_conversation_memory(
                "Remember this",
                "general",
                storage_path,
                "session",
                config,
                runtime_paths_for(config),
            )

        assert get_embedder_failure() == "embedder authentication failed (HTTP 401)"
        assert "embedder authentication failed (HTTP 401)" in str(logs)
        assert "Conversation memory was not stored in any target" in str(logs)
        assert "Memory added" not in str(logs)
    finally:
        capture_embedder_health_recorder().record(None)


@pytest.mark.asyncio
async def test_private_agent_explicit_mem0_uses_private_instance_storage(
    storage_path: Path,
    config: Config,
) -> None:
    """Explicit mem0 CRUD for private agents should stay inside the private-instance root."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths, timing_scope
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=scope_storage_path.name))

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
        tool_execution_identity(execution_identity),
    ):
        await add_agent_memory(
            "Private note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        search_results = await _search_memory_results(
            "Private note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
            limit=10,
        )
        listed = await list_all_agent_memories(
            "general",
            storage_path,
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
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert loaded is not None

        await update_agent_memory(
            memory_id,
            "Updated private note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        updated = await get_agent_memory(
            memory_id,
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert updated is not None
        assert updated["memory"] == "Updated private note"

        await delete_agent_memory(
            memory_id,
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        deleted = await get_agent_memory(
            memory_id,
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            execution_identity=execution_identity,
        )
        assert deleted is None

    expected_private_path = private_instance_scope_root_path(storage_path, worker_key) / "general"
    assert set(memories_by_path) == {expected_private_path}
    assert agent_state_root_path(storage_path, "general") not in memories_by_path
    assert any(result.get("memory") == "Private note" for result in search_results)


@pytest.mark.asyncio
async def test_mem0_team_conversation_memory_is_shared_across_requesters_for_user_scoped_workers(
    storage_path: Path,
    config: Config,
) -> None:
    """User-scoped workers should still share one durable team memory view per agent set."""
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

    stored_memories: dict[tuple[Path, str], list[dict[str, object]]] = {}

    class FakeScopedMemory:
        def __init__(self, scope_storage_path: Path) -> None:
            self.scope_storage_path = scope_storage_path

        async def add(self, messages: list[dict], user_id: str, metadata: dict) -> None:
            entry = {
                "id": f"{user_id}-{len(stored_memories)}",
                "memory": " ".join(str(message["content"]).strip() for message in messages if message.get("content")),
                "user_id": user_id,
                "metadata": metadata,
            }
            stored_memories.setdefault((self.scope_storage_path, user_id), []).append(entry)

        async def search(
            self,
            query: str,
            *,
            filters: dict[str, object],
            top_k: int = 3,
        ) -> dict[str, list[dict[str, object]]]:
            user_id = filters["user_id"]
            matches = [
                dict(entry)
                for entry in stored_memories.get((self.scope_storage_path, user_id), [])
                if query.lower() in str(entry["memory"]).lower()
            ]
            return {"results": matches[:top_k]}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeScopedMemory:
        del runtime_paths, timing_scope
        return FakeScopedMemory(scope_storage_path)

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_fake_memory_instance):
        with tool_execution_identity(alice_identity):
            await store_conversation_memory(
                "Alice-authored shared team memory",
                ["general", "calculator"],
                storage_path,
                "session-alice",
                config,
                runtime_paths_for(config),
            )
            alice_results = await _search_memory_results(
                "Alice-authored shared team",
                "general",
                storage_path,
                config,
                runtime_paths_for(config),
                limit=5,
            )

        with tool_execution_identity(bob_identity):
            bob_results = await _search_memory_results(
                "Alice-authored shared team",
                "general",
                storage_path,
                config,
                runtime_paths_for(config),
                limit=5,
            )

    assert any(result.get("memory") == "Alice-authored shared team memory" for result in alice_results)
    assert any(result.get("memory") == "Alice-authored shared team memory" for result in bob_results)

    assert (agent_state_root_path(storage_path, "general"), "team_calculator+general") in stored_memories
    assert (agent_state_root_path(storage_path, "calculator"), "team_calculator+general") in stored_memories
    assert (storage_path, "team_calculator+general") not in stored_memories


@pytest.mark.asyncio
async def test_mixed_private_team_mem0_conversation_memory_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """Mem0 team memory should reject private team members outright."""
    config.memory.backend = "mem0"
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    with pytest.raises(
        ValueError,
        match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
    ):
        await store_conversation_memory(
            "Alice-authored private team memory",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
        )


@pytest.mark.asyncio
async def test_mixed_private_team_mem0_member_crud_is_rejected(
    storage_path: Path,
    config: Config,
) -> None:
    """Mem0 team member CRUD should reject private team members."""
    config.memory.backend = "mem0"
    config.memory.team_reads_member_memory = True
    config.agents["general"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.teams = {"mixed_team": MockTeamConfig(agents=["general", "calculator"])}

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths, timing_scope
        id_prefix = scope_storage_path.name.replace("/", "_") or "mem"
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=id_prefix))

    with patch("mindroom.memory._backend.create_memory_instance", side_effect=create_fake_memory_instance):
        await add_agent_memory("Shared calculator note", "calculator", storage_path, config, runtime_paths_for(config))
        calculator_memory_id = (
            await list_all_agent_memories("calculator", storage_path, config, runtime_paths_for(config), limit=10)
        )[0]["id"]

        with pytest.raises(
            ValueError,
            match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        ):
            await get_agent_memory(
                calculator_memory_id,
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

        with pytest.raises(
            ValueError,
            match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        ):
            await update_agent_memory(
                calculator_memory_id,
                "Updated shared calculator note",
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

        with pytest.raises(
            ValueError,
            match="private agents are only supported in explicit Matrix ad hoc teams with requester identity",
        ):
            await delete_agent_memory(
                calculator_memory_id,
                ["general", "calculator"],
                storage_path,
                config,
                runtime_paths_for(config),
            )

    assert agent_state_root_path(storage_path, "calculator") in memories_by_path


@pytest.mark.asyncio
async def test_worker_scoped_team_mem0_memory_can_be_read_updated_and_deleted_across_worker_roots(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "mem0"
    config.agents["general"].worker_scope = "user_agent"
    config.agents["calculator"].worker_scope = "user_agent"
    config.teams = {"gc": MockTeamConfig(agents=["general", "calculator"])}

    memories_by_path: dict[Path, FakeMem0ScopedMemory] = {}

    async def create_fake_memory_instance(
        scope_storage_path: Path,
        _config: Config,
        *,
        runtime_paths: object,
        timing_scope: str | None = None,
    ) -> FakeMem0ScopedMemory:
        del runtime_paths, timing_scope
        id_prefix = scope_storage_path.name.replace("/", "_") or "mem"
        return memories_by_path.setdefault(scope_storage_path, FakeMem0ScopedMemory(id_prefix=id_prefix))

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
        patch("mindroom.memory._backend.create_memory_instance", side_effect=create_fake_memory_instance),
        tool_execution_identity(execution_identity),
    ):
        await store_conversation_memory(
            "Team shared note",
            ["general", "calculator"],
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
        )

        general_results = await _search_memory_results(
            "shared note",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_results = await _search_memory_results(
            "shared note",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert len(general_results) == 1
        assert len(calculator_results) == 1
        general_memory_id = general_results[0]["id"]
        calculator_memory_id = calculator_results[0]["id"]
        assert general_memory_id != calculator_memory_id

        general_loaded = await get_agent_memory(
            general_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )
        calculator_loaded = await get_agent_memory(
            calculator_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )
        assert general_loaded is not None
        assert calculator_loaded is not None
        assert general_loaded["memory"] == "Team shared note"
        assert calculator_loaded["memory"] == "Team shared note"

        await update_agent_memory(
            calculator_memory_id,
            "Updated team shared note",
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )

        general_updated = await _search_memory_results(
            "updated team",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_updated = await _search_memory_results(
            "updated team",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert any(result.get("memory") == "Updated team shared note" for result in general_updated)
        assert any(result.get("memory") == "Updated team shared note" for result in calculator_updated)

        await delete_agent_memory(
            general_memory_id,
            ["general", "calculator"],
            storage_path,
            config,
            runtime_paths_for(config),
        )

        general_deleted = await _search_memory_results(
            "team",
            "general",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        calculator_deleted = await _search_memory_results(
            "team",
            "calculator",
            storage_path,
            config,
            runtime_paths_for(config),
            limit=10,
        )
        assert not any(result.get("memory") == "Team shared note" for result in general_deleted)
        assert not any(result.get("memory") == "Team shared note" for result in calculator_deleted)
        assert not any(result.get("memory") == "Updated team shared note" for result in general_deleted)
        assert not any(result.get("memory") == "Updated team shared note" for result in calculator_deleted)


def _mem0_memory_reporting_usage(config: Config) -> Callable[..., SimpleNamespace]:
    """Return a Mem0 factory whose memories run one extraction call reporting 120 input and 30 output tokens per add."""
    config.memory.backend = "mem0"
    config.memory.llm = _MemoryLLMConfig(provider="openai", config={"api_key": "test-key", "model": "gpt-6-luna"})
    usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30, prompt_tokens_details=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))], usage=usage)

    def from_config(*_args: object, **_kwargs: object) -> SimpleNamespace:
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=MagicMock(return_value=response))),
        )
        with patch("mem0.llms.openai.OpenAI", return_value=client):
            mem0_llm = OpenAILLM(config.memory.llm.config)

        async def add(messages: list[dict], **_: object) -> None:
            # Mem0 runs its extraction prompt in a worker thread.
            await asyncio.to_thread(mem0_llm.generate_response, messages=messages)

        return SimpleNamespace(llm=mem0_llm, vector_store=object(), add=add)

    return from_config


@pytest.mark.asyncio
async def test_conversation_memory_extraction_records_its_token_usage(storage_path: Path, config: Config) -> None:
    """Mem0 calls its own LLM client, so its extraction tokens are recorded for the conversation that produced them."""
    fake_memory = _mem0_memory_reporting_usage(config)
    recorded: list[tuple[RunOutput, str, str, str | None]] = []

    async def record(
        response: RunOutput,
        *,
        owner: HelperUsageOwner,
        kind: str,
        requester_id: str | None,
        **_: object,
    ) -> None:
        recorded.append((response, owner.session_id, kind, requester_id))

    with (
        patch.object(AsyncMemory, "from_config", side_effect=fake_memory),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=record),
    ):
        await store_conversation_memory(
            "Remember that Atlas ships Friday.",
            "general",
            storage_path,
            "session-alice",
            config,
            runtime_paths_for(config),
            user_id="@alice:example.org",
        )

    [(run, session_id, kind, requester_id)] = recorded
    assert (session_id, kind, requester_id) == ("session-alice", "memory_extraction", "@alice:example.org")
    assert run.metrics is not None
    assert (run.metrics.input_tokens, run.metrics.output_tokens, run.metrics.total_tokens) == (120, 30, 150)


@pytest.mark.asyncio
async def test_team_memory_extraction_counts_as_internal_usage(storage_path: Path, config: Config) -> None:
    """A team conversation has no single agent session, so its extraction is reported as internal work."""
    config.teams = {"shared_team": MockTeamConfig(agents=["general", "calculator"])}
    fake_stores = MagicMock(side_effect=_mem0_memory_reporting_usage(config))
    recorded: list[tuple[RunOutput, str, str | None]] = []

    async def record(response: RunOutput, *, kind: str, requester_id: str | None = None, **_: object) -> None:
        recorded.append((response, kind, requester_id))

    with (
        patch.object(AsyncMemory, "from_config", fake_stores),
        patch("mindroom.memory._mem0_backend.record_system_usage", side_effect=record),
    ):
        await store_conversation_memory(
            "Remember that Atlas ships Friday.",
            ["general", "calculator"],
            storage_path,
            "team-session",
            config,
            runtime_paths_for(config),
            user_id="@alice:example.org",
        )

    [(run, kind, requester_id)] = recorded
    assert (kind, requester_id) == ("memory_extraction", "@alice:example.org")
    assert run.metrics is not None
    # Team memory is extracted once into each store it is kept in.
    assert run.metrics.total_tokens == 150 * fake_stores.call_count


@pytest.mark.asyncio
async def test_memory_tool_additions_record_usage_for_the_reply_that_made_them(
    storage_path: Path,
    config: Config,
) -> None:
    """The memory tool runs inside a reply, so its extraction tokens go to that reply's conversation."""
    fake_memory = _mem0_memory_reporting_usage(config)
    owner = HelperUsageOwner(storage_factory=MagicMock(), session_id="reply-session")
    recorded: list[tuple[HelperUsageOwner, str, int]] = []

    async def record(response: RunOutput, *, owner: HelperUsageOwner, kind: str, **_: object) -> None:
        assert response.metrics is not None
        recorded.append((owner, kind, response.metrics.total_tokens))

    with (
        patch.object(AsyncMemory, "from_config", side_effect=fake_memory),
        patch("mindroom.memory._mem0_backend.record_helper_usage", side_effect=record),
        helper_usage_owner_context(owner),
    ):
        await add_agent_memory("Atlas ships Friday.", "general", storage_path, config, runtime_paths_for(config))

    assert recorded == [(owner, "memory_extraction", 150)]


@pytest.mark.parametrize(
    ("provider", "client", "expected"),
    [
        pytest.param(
            "openai",
            SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(
                        create=lambda **_: SimpleNamespace(
                            usage=SimpleNamespace(
                                prompt_tokens=100,
                                completion_tokens=20,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                            ),
                        ),
                    ),
                ),
            ),
            (100, 20, 40),
            id="openai",
        ),
        pytest.param(
            "anthropic",
            SimpleNamespace(
                messages=SimpleNamespace(
                    create=lambda **_: SimpleNamespace(
                        usage=SimpleNamespace(input_tokens=90, output_tokens=10, cache_read_input_tokens=30),
                    ),
                ),
            ),
            (90, 10, 30),
            id="anthropic",
        ),
        pytest.param(
            "ollama",
            SimpleNamespace(chat=lambda **_: SimpleNamespace(prompt_eval_count=70, eval_count=5)),
            (70, 5, 0),
            id="ollama",
        ),
    ],
)
def test_each_provider_reports_its_extraction_tokens(
    provider: str,
    client: SimpleNamespace,
    expected: tuple[int, int, int],
) -> None:
    """Each provider SDK reports usage in its own shape."""
    llm = SimpleNamespace(config=SimpleNamespace(model="extractor"), client=client)
    install_mem0_usage_capture(llm, provider)
    install_mem0_usage_capture(llm, provider)  # Installing again must not count calls twice.
    call = {
        "openai": lambda: llm.client.chat.completions.create(),
        "anthropic": lambda: llm.client.messages.create(),
        "ollama": lambda: llm.client.chat(),
    }[provider]

    call()  # Outside a memory write, nothing is collected.
    with counted_mem0_usage() as calls:
        call()

    run = mem0_usage_run(calls)
    assert run is not None
    assert run.metrics is not None
    assert (run.metrics.input_tokens, run.metrics.output_tokens, run.metrics.cache_read_tokens) == expected
