"""Tests for the explicit memory tool (MemoryTools toolkit)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import httpx
import ollama
import pytest
from openai import AuthenticationError

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.memory import MemoryTools
from mindroom.embeddings.errors import EmbedderRequestError, openai_embedder_error
from mindroom.embeddings.ollama_embedder import OllamaEmbedder
from mindroom.memory import MemoryNotFoundError, MemorySearchOutcome, search_agent_memories
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from tests.conftest import bind_runtime_paths, runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _auth_error_with_secret(secret: str) -> EmbedderRequestError:
    """Return what the embedder raises for a rejected key, classified at its boundary from the SDK error."""
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(401, request=request, json={"error": {"message": f"rejected {secret}"}})
    return openai_embedder_error(AuthenticationError(f"rejected {secret}", response=response, body=None))


class TestMemoryTools:
    """Tests for the MemoryTools Toolkit."""

    @pytest.fixture
    def storage_path(self, tmp_path: Path) -> Path:
        """Create a temporary storage path."""
        return tmp_path

    @pytest.fixture
    def config(self, storage_path: Path) -> Config:
        """Load a self-contained runtime-bound config for testing."""
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
                    "test_agent": AgentConfig(display_name="Test Agent"),
                    "general": AgentConfig(display_name="General"),
                },
            ),
            runtime_paths,
        )

    @pytest.fixture
    def tools(self, config: Config) -> MemoryTools:
        """Create a MemoryTools instance for testing."""
        return MemoryTools(
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

    @pytest.fixture
    def similar_lookup(self) -> Iterator[AsyncMock]:
        """Keep add_memory's similar-memory lookup off the real default mem0 backend."""
        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=MemorySearchOutcome(results=[]),
        ) as mock_search:
            yield mock_search

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("similar_lookup")
    async def test_add_memory(self, tools: MemoryTools) -> None:
        """Test that add_memory stores content via add_agent_memory."""
        with patch("mindroom.custom_tools.memory.add_agent_memory", new_callable=AsyncMock) as mock_add:
            result = await tools.memory(action="add", content="The user prefers dark mode")

            mock_add.assert_called_once_with(
                "The user prefers dark mode",
                "test_agent",
                tools._config,
                tools._runtime_paths,
                metadata={"source": "explicit_tool"},
                execution_identity=None,
            )
            assert "Memorized" in result
            assert "dark mode" in result

    @pytest.mark.asyncio
    async def test_add_memory_uses_stored_execution_identity(
        self,
        config: Config,
        similar_lookup: AsyncMock,
    ) -> None:
        """MemoryTools should forward the constructor-bound execution identity without ambient context."""
        execution_identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="test_agent",
            requester_id="@alice:example.org",
            room_id="!room:example.org",
            thread_id="$thread",
            resolved_thread_id="$thread",
            session_id="session-1",
        )
        tools = MemoryTools(
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths_for(config),
            execution_identity=execution_identity,
        )

        with patch("mindroom.custom_tools.memory.add_agent_memory", new_callable=AsyncMock) as mock_add:
            await tools.memory(action="add", content="Remember this")

        assert mock_add.await_args.kwargs["execution_identity"] == execution_identity
        assert similar_lookup.await_args.kwargs["execution_identity"] == execution_identity

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("similar_lookup")
    async def test_add_memory_error(self, tools: MemoryTools) -> None:
        """Test that add_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.add_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.memory(action="add", content="something")

            assert "Failed to store memory" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("similar_lookup")
    async def test_add_memory_redacts_provider_error(self, tools: MemoryTools) -> None:
        """A rejected embedder key never reaches Matrix-visible tool output."""
        secret = "sk-secret-add"  # noqa: S105
        with patch(
            "mindroom.custom_tools.memory.add_agent_memory",
            new_callable=AsyncMock,
            side_effect=_auth_error_with_secret(secret),
        ):
            result = await tools.memory(action="add", content="something")

        assert result == "Failed to store memory: embedder authentication failed (HTTP 401)"
        assert secret not in result

    @pytest.mark.asyncio
    async def test_add_memory_uses_same_agent_file_memory_root_as_prompt_reads(
        self,
        storage_path: Path,
        config: Config,
    ) -> None:
        """Explicit memory writes should land in the same canonical files prompt reads use."""
        config.memory.backend = "file"
        config.agents["general"].memory_backend = "file"

        tools = MemoryTools(
            agent_name="general",
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        result = await tools.memory(action="add", content="Tool memory stays canonical")
        memories = (
            await search_agent_memories(
                "Tool memory",
                "general",
                config,
                runtime_paths_for(config),
                limit=5,
            )
        ).results

        assert result == "Memorized: Tool memory stays canonical"
        assert any(memory.get("memory") == "Tool memory stays canonical" for memory in memories)
        assert (agent_workspace_root_path(storage_path, "general") / "MEMORY.md").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("search_mode", ["keyword", "semantic"])
    async def test_add_memory_lists_similar_existing_memories(
        self,
        config: Config,
        search_mode: str,
    ) -> None:
        """A new memory should point at related existing entries so corrections update them instead of piling up.

        A healthy semantic index returns read-only locators, so the lookup must still find the editable entry.
        The agent rewrites a listed entry whole with update_memory, so the reply shows each one in full, and it never
        asks to delete an entry the reply does not show.
        """
        config.memory.backend = "file"
        config.memory.search.mode = search_mode
        config.agents["general"].memory_backend = "file"
        tools = MemoryTools(
            agent_name="general",
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        existing_text = (
            "The weekly standup is on Mondays at 09:30 in room 4B, hosted by the platform team; agenda items go in the "
            "shared doc by Friday, the demo slot is the last ten minutes, and remote attendees join the video room"
        )
        semantic_hit = {"id": "semantic:memory/notes.md:0", "memory": existing_text}

        with patch(
            "mindroom.memory._file_backend.search_semantic_file_memories",
            new_callable=AsyncMock,
            return_value=[semantic_hit],
        ):
            await tools.memory(action="add", content=existing_text)
            existing = (await tools.memory(action="list")).splitlines()[1]
            existing_id = existing.split("[id=", 1)[1].split("]", 1)[0]

            result = await tools.memory(action="add", content="The weekly standup moved to Tuesdays")

        assert result.startswith("Memorized: The weekly standup moved to Tuesdays\n")
        assert f"[id={existing_id}] {existing_text}" in result
        assert result.count("moved to Tuesdays") == 1
        assert 'memory(action="update", ...)' in result
        assert 'action="delete"' not in result

    @pytest.mark.asyncio
    async def test_add_memory_reply_stays_plain_when_the_similar_lookup_fails(self, tools: MemoryTools) -> None:
        """A failed similar-memory lookup never fails the add."""
        with (
            patch("mindroom.custom_tools.memory.add_agent_memory", new_callable=AsyncMock),
            patch(
                "mindroom.custom_tools.memory.search_agent_memories",
                new_callable=AsyncMock,
                side_effect=RuntimeError("index down"),
            ),
        ):
            result = await tools.memory(action="add", content="The user prefers dark mode")

        assert result == "Memorized: The user prefers dark mode"

    @pytest.mark.asyncio
    async def test_search_memories(self, tools: MemoryTools) -> None:
        """Test that search_memories calls search_agent_memories and formats results."""
        mock_results = [
            {"id": "abc-1", "memory": "User likes Python", "score": 0.9},
            {"id": "abc-2", "memory": "User prefers dark mode", "score": 0.8},
        ]

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=MemorySearchOutcome(results=mock_results),
        ) as mock_search:
            result = await tools.memory(action="search", query="preferences", limit=3)

            mock_search.assert_called_once_with(
                "preferences",
                "test_agent",
                tools._config,
                tools._runtime_paths,
                limit=3,
                execution_identity=None,
            )
            assert "Found 2 memory(ies)" in result
            assert "[id=abc-1]" in result
            assert "User likes Python" in result
            assert "[id=abc-2]" in result
            assert "User prefers dark mode" in result

    def test_search_memories_tool_description_is_backend_neutral(self, tools: MemoryTools) -> None:
        """Agent-facing memory search should not mention a specific storage backend."""
        description = tools.memory.__doc__

        assert description is not None
        assert "notes" in description
        assert "configured Markdown paths" in description
        assert "default: memory/**/*.md" in description
        assert "falls back to keyword search" in description
        assert "MEMORY.md and memory/**/*.md" in description
        assert "semantic:*" in description
        assert "read-only locators" in description
        assert "`get`, `update`, or `delete`" in description
        assert "knowledge base" not in description.lower()
        assert "mem0" not in description.lower()

    @pytest.mark.asyncio
    async def test_search_memories_marks_result_modes(self, tools: MemoryTools) -> None:
        """Search results should make semantic-vs-keyword mode visible when available."""
        mock_results = [
            {
                "id": "semantic:file.md:1",
                "memory": "Semantic match",
                "metadata": {"search_mode": "semantic"},
            },
            {
                "id": "file:MEMORY.md:2",
                "memory": "Keyword match",
                "metadata": {"search_mode": "keyword"},
            },
        ]

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=MemorySearchOutcome(results=mock_results),
        ):
            result = await tools.memory(action="search", query="preferences")

        assert "[id=semantic:file.md:1] [semantic] Semantic match" in result
        assert "[id=file:MEMORY.md:2] [keyword] Keyword match" in result

    @pytest.mark.asyncio
    async def test_search_memories_empty(self, tools: MemoryTools) -> None:
        """Test that search_memories returns a message when no results found."""
        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=MemorySearchOutcome(results=[]),
        ):
            result = await tools.memory(action="search", query="nonexistent")

            assert result == "No relevant memories found."

    @pytest.mark.asyncio
    async def test_search_memories_auth_degraded_without_matches(self, tools: MemoryTools) -> None:
        """A confirmed auth failure with no fallback matches reports the exact failure line plus advice."""
        outcome = MemorySearchOutcome(results=[], degraded_reason="embedder authentication failed (HTTP 401)")

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        lines = result.splitlines()
        assert lines[0] == "Semantic memory search unavailable: embedder authentication failed (HTTP 401)."
        assert "memory.embedder.config.api_key" in result
        assert "'embedder' credential" in result
        assert "EMBEDDER_API_KEY" in result

    @pytest.mark.asyncio
    async def test_search_memories_auth_degraded_with_keyword_matches(self, tools: MemoryTools) -> None:
        """Keyword fallback matches keep flowing under the failure line."""
        outcome = MemorySearchOutcome(
            results=[{"id": "k1", "memory": "Keyword hit", "metadata": {"search_mode": "keyword"}}],
            degraded_reason="embedder authentication failed (HTTP 401)",
        )

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        lines = result.splitlines()
        assert lines[0] == (
            "Semantic memory search unavailable: embedder authentication failed (HTTP 401). "
            "Showing keyword matches only."
        )
        assert "EMBEDDER_API_KEY" in result
        assert "Found 1 memory(ies):" in result
        assert "[id=k1] [keyword] Keyword hit" in result

    @pytest.mark.asyncio
    async def test_search_memories_non_auth_degraded_has_no_credential_advice(self, tools: MemoryTools) -> None:
        """Non-credential causes surface the safe detail without credential advice."""
        outcome = MemorySearchOutcome(results=[], degraded_reason="embedder endpoint unreachable")

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        assert result == "Semantic memory search unavailable: embedder endpoint unreachable."
        assert "EMBEDDER_API_KEY" not in result
        assert "credential" not in result

    @pytest.mark.asyncio
    async def test_search_memories_non_auth_degraded_with_keyword_matches(self, tools: MemoryTools) -> None:
        """Non-credential causes with fallback matches keep the punctuated failure line."""
        outcome = MemorySearchOutcome(
            results=[{"id": "k1", "memory": "Keyword hit", "metadata": {"search_mode": "keyword"}}],
            degraded_reason="embedder endpoint unreachable",
        )

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        lines = result.splitlines()
        assert lines[0] == (
            "Semantic memory search unavailable: embedder endpoint unreachable. Showing keyword matches only."
        )
        assert "credential" not in result
        assert "[id=k1] [keyword] Keyword hit" in result

    @pytest.mark.asyncio
    async def test_search_memories_partial_semantic_results_are_not_called_keyword_only(
        self,
        tools: MemoryTools,
    ) -> None:
        """A failed Mem0 scope can coexist with semantic results from healthy scopes."""
        outcome = MemorySearchOutcome(
            results=[{"id": "m1", "memory": "Healthy scope hit", "metadata": {}}],
            degraded_reason="embedder endpoint unreachable",
        )

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        assert result.splitlines()[0] == (
            "Semantic memory search partially unavailable: embedder endpoint unreachable. "
            "Showing results from available scopes."
        )
        assert "keyword matches only" not in result

    @pytest.mark.asyncio
    async def test_credential_advice_names_the_configured_service(
        self,
        config: Config,
    ) -> None:
        """Auth guidance should identify the strict credential binding that can recover the embedder."""
        runtime_paths = runtime_paths_for(config)
        config = Config.model_validate(
            {
                **config.model_dump(),
                "memory": {
                    **config.memory.model_dump(),
                    "embedder": {
                        "provider": "openai",
                        "config": {
                            "model": "text-embedding-3-small",
                            "credentials_service": "embedding-production",
                        },
                    },
                },
            },
        )
        tools = MemoryTools(
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths,
        )
        outcome = MemorySearchOutcome(results=[], degraded_reason="embedder authentication failed (HTTP 401)")

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        assert "memory.embedder.config.api_key" in result
        assert "'embedding-production' credential service" in result
        assert "EMBEDDER_API_KEY" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [401, 403])
    async def test_credential_advice_for_an_ollama_rejection_names_the_key_ollama_sends(
        self,
        config: Config,
        status_code: int,
    ) -> None:
        """An Ollama embedder sends only OLLAMA_API_KEY, so its advice names no setting that embedder ignores."""
        runtime_paths = runtime_paths_for(config)
        config = Config.model_validate(
            {
                **config.model_dump(),
                "memory": {
                    **config.memory.model_dump(),
                    "embedder": {"provider": "ollama", "config": {"model": "nomic-embed-text"}},
                },
            },
        )
        transport = httpx.MockTransport(lambda _request: httpx.Response(status_code, json={"error": "unauthorized"}))
        embedder = OllamaEmbedder(
            model="nomic-embed-text",
            host="http://ollama.local:11434",
            client=ollama.Client(host="http://ollama.local:11434", transport=transport),
        )
        with pytest.raises(EmbedderRequestError) as rejection:
            embedder.embed(["anything"], purpose="query")
        tools = MemoryTools(
            agent_name="test_agent",
            config=config,
            runtime_paths=runtime_paths,
        )
        outcome = MemorySearchOutcome(results=[], degraded_reason=str(rejection.value))

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        assert result.splitlines() == [
            f"Semantic memory search unavailable: {rejection.value}.",
            "Repair the embedder credential: the Ollama embedder sends only OLLAMA_API_KEY, "
            "so set it in the environment of the MindRoom process and restart MindRoom.",
        ]

    @pytest.mark.asyncio
    async def test_credential_advice_for_a_vertex_ai_rejection_names_google_cloud_credentials(
        self,
        config: Config,
    ) -> None:
        """A Vertex AI embedder authenticates with Google Cloud credentials, so its advice names no API key."""
        runtime_paths = runtime_paths_for(config)
        config = Config.model_validate(
            {
                **config.model_dump(),
                "memory": {
                    **config.memory.model_dump(),
                    "backend": "file",
                    "embedder": {"provider": "vertex_ai", "config": {"model": "gemini-embedding-2"}},
                },
            },
        )
        tools = MemoryTools(agent_name="test_agent", config=config, runtime_paths=runtime_paths)
        outcome = MemorySearchOutcome(results=[], degraded_reason="embedder permission denied (HTTP 403)")

        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            return_value=outcome,
        ):
            result = await tools.memory(action="search", query="anything")

        assert result.splitlines() == [
            "Semantic memory search unavailable: embedder permission denied (HTTP 403).",
            "Repair the embedder credential: the Vertex AI embedder authenticates with GOOGLE_APPLICATION_CREDENTIALS "
            "or Application Default Credentials, which need permission to call Vertex AI in its project, and then "
            "restart MindRoom.",
        ]

    @pytest.mark.asyncio
    async def test_search_memories_error(self, tools: MemoryTools) -> None:
        """Test that search_memories handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.search_agent_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Search failed"),
        ):
            result = await tools.memory(action="search", query="anything")

            assert "Failed to search memories" in result
            assert "Search failed" in result

    @pytest.mark.asyncio
    async def test_update_memory_redacts_provider_error(self, tools: MemoryTools) -> None:
        """Update failures use the same credential-safe provider detail."""
        secret = "sk-secret-update"  # noqa: S105
        with patch(
            "mindroom.custom_tools.memory.update_agent_memory",
            new_callable=AsyncMock,
            side_effect=_auth_error_with_secret(secret),
        ):
            result = await tools.memory(action="update", memory_id="memory-id", content="new content")

        assert result == "Failed to update memory: embedder authentication failed (HTTP 401)"
        assert secret not in result

    def test_toolkit_name(self, tools: MemoryTools) -> None:
        """Test that the toolkit is registered with the correct name."""
        assert tools.name == "memory"

    def test_toolkit_exposes_unified_memory_tool(self, tools: MemoryTools) -> None:
        """Test that the toolkit exposes only the single unified memory tool."""
        assert list(tools.functions) == ["memory"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"action": "add"}, "`content` is required"),
            ({"action": "add", "content": "  "}, "`content` is required"),
            ({"action": "add", "content": "fact", "query": "q"}, "`query`"),
            ({"action": "add", "content": "fact", "memory_id": "m1"}, "`memory_id`"),
            ({"action": "add", "content": "fact", "limit": 5}, "`limit`"),
            ({"action": "search"}, "`query` is required"),
            ({"action": "search", "query": "  "}, "`query` is required"),
            ({"action": "search", "query": "q", "content": "c"}, "`content`"),
            ({"action": "search", "query": "q", "memory_id": "m1"}, "`memory_id`"),
            ({"action": "list", "content": "c"}, "`content`"),
            ({"action": "list", "query": "q"}, "`query`"),
            ({"action": "list", "memory_id": "m1"}, "`memory_id`"),
            ({"action": "get"}, "`memory_id` is required"),
            ({"action": "get", "memory_id": "m1", "content": "c"}, "`content`"),
            ({"action": "get", "memory_id": "m1", "query": "q"}, "`query`"),
            ({"action": "get", "memory_id": "m1", "limit": 5}, "`limit`"),
            ({"action": "update", "content": "c"}, "`memory_id` is required"),
            ({"action": "update", "memory_id": "m1"}, "`content` is required"),
            ({"action": "update", "memory_id": "m1", "content": "c", "query": "q"}, "`query`"),
            ({"action": "update", "memory_id": "m1", "content": "c", "limit": 5}, "`limit`"),
            ({"action": "delete"}, "`memory_id` is required"),
            ({"action": "delete", "memory_id": "m1", "content": "c"}, "`content`"),
            ({"action": "delete", "memory_id": "m1", "query": "q"}, "`query`"),
            ({"action": "delete", "memory_id": "m1", "limit": 5}, "`limit`"),
        ],
    )
    async def test_memory_action_parameter_validation(
        self,
        tools: MemoryTools,
        kwargs: dict[str, object],
        expected: str,
    ) -> None:
        """Missing required arguments or mismatched arguments return clear error strings."""
        result = await tools.memory(**kwargs)  # type: ignore[arg-type]
        assert result.startswith("Error:")
        assert expected in result

    @pytest.mark.asyncio
    async def test_list_memories(self, tools: MemoryTools) -> None:
        """Test that list_memories calls list_all_agent_memories and formats results."""
        mock_results = [
            {"id": "m1", "memory": "User likes Python"},
            {"id": "m2", "memory": "User prefers dark mode"},
            {"id": "m3", "memory": "Project uses FastAPI"},
        ]

        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            return_value=mock_results,
        ) as mock_list:
            result = await tools.memory(action="list", limit=10)

            mock_list.assert_called_once_with(
                "test_agent",
                tools._config,
                tools._runtime_paths,
                limit=10,
                execution_identity=None,
            )
            assert "All memories (3)" in result
            assert "[id=m1]" in result
            assert "User likes Python" in result
            assert "[id=m2]" in result
            assert "User prefers dark mode" in result
            assert "[id=m3]" in result
            assert "Project uses FastAPI" in result

    @pytest.mark.asyncio
    async def test_list_memories_empty(self, tools: MemoryTools) -> None:
        """Test that list_memories returns a message when no memories exist."""
        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await tools.memory(action="list")

            assert result == "No memories stored yet."

    @pytest.mark.asyncio
    async def test_list_memories_error(self, tools: MemoryTools) -> None:
        """Test that list_memories handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.list_all_agent_memories",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.memory(action="list")

            assert "Failed to list memories" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    async def test_get_memory(self, tools: MemoryTools) -> None:
        """Test that get_memory retrieves a single memory by ID."""
        mock_result = {"id": "abc-123", "memory": "User likes Python"}

        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_get:
            result = await tools.memory(action="get", memory_id="abc-123")

            mock_get.assert_called_once_with(
                "abc-123",
                "test_agent",
                tools._config,
                tools._runtime_paths,
                execution_identity=None,
            )
            assert "[id=abc-123]" in result
            assert "User likes Python" in result

    @pytest.mark.asyncio
    async def test_get_memory_not_found(self, tools: MemoryTools) -> None:
        """Test that get_memory returns a message when memory not found."""
        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await tools.memory(action="get", memory_id="nonexistent")

            assert "No memory found" in result

    @pytest.mark.asyncio
    async def test_get_memory_error(self, tools: MemoryTools) -> None:
        """Test that get_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.get_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB down"),
        ):
            result = await tools.memory(action="get", memory_id="abc-123")

            assert "Failed to get memory" in result
            assert "DB down" in result

    @pytest.mark.asyncio
    async def test_update_memory(self, tools: MemoryTools) -> None:
        """Test that update_memory updates a memory by ID."""
        with patch(
            "mindroom.custom_tools.memory.update_agent_memory",
            new_callable=AsyncMock,
        ) as mock_update:
            result = await tools.memory(action="update", memory_id="abc-123", content="Updated content")

            mock_update.assert_called_once_with(
                "abc-123",
                "Updated content",
                "test_agent",
                tools._config,
                tools._runtime_paths,
                execution_identity=None,
            )
            assert "Updated memory" in result
            assert "[id=abc-123]" in result
            assert "Updated content" in result

    @pytest.mark.asyncio
    async def test_update_memory_error(self, tools: MemoryTools) -> None:
        """Unexpected update failures return the error text and log a full exception."""
        with (
            patch(
                "mindroom.custom_tools.memory.update_agent_memory",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Not found"),
            ),
            patch("mindroom.custom_tools.memory.logger") as mock_logger,
        ):
            result = await tools.memory(action="update", memory_id="abc-123", content="new content")

        assert result == "Failed to update memory: Not found"
        mock_logger.exception.assert_called_once_with(
            "Failed to update memory via tool",
            error_type="RuntimeError",
            agent="test_agent",
            memory_id="abc-123",
        )
        mock_logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_memory(self, tools: MemoryTools) -> None:
        """Test that delete_memory deletes a memory by ID."""
        with patch(
            "mindroom.custom_tools.memory.delete_agent_memory",
            new_callable=AsyncMock,
        ) as mock_delete:
            result = await tools.memory(action="delete", memory_id="abc-123")

            mock_delete.assert_called_once_with(
                "abc-123",
                "test_agent",
                tools._config,
                tools._runtime_paths,
                execution_identity=None,
            )
            assert "Deleted memory" in result
            assert "[id=abc-123]" in result

    @pytest.mark.asyncio
    async def test_delete_memory_error(self, tools: MemoryTools) -> None:
        """Test that delete_memory handles errors gracefully."""
        with patch(
            "mindroom.custom_tools.memory.delete_agent_memory",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Not found"),
        ):
            result = await tools.memory(action="delete", memory_id="abc-123")

            assert "Failed to delete memory" in result
            assert "Not found" in result

    @pytest.mark.asyncio
    async def test_memory_not_found_logs_warning_without_traceback(self, tools: MemoryTools) -> None:
        """Missing memory IDs log a warning without a traceback and preserve the tool error text."""
        cases = (
            (
                "update",
                "update_agent_memory",
                lambda: tools.memory(action="update", memory_id="missing-id", content="new content"),
            ),
            ("delete", "delete_agent_memory", lambda: tools.memory(action="delete", memory_id="missing-id")),
        )
        for action, helper_name, invoke in cases:
            with (
                patch(
                    f"mindroom.custom_tools.memory.{helper_name}",
                    new_callable=AsyncMock,
                    side_effect=MemoryNotFoundError("missing-id"),
                ),
                patch("mindroom.custom_tools.memory.logger") as mock_logger,
            ):
                assert await invoke() == f"Failed to {action} memory: No memory found with id=missing-id"
            mock_logger.warning.assert_called_once_with(
                f"Failed to {action} memory via tool",
                error="No memory found with id=missing-id",
                agent="test_agent",
                memory_id="missing-id",
            )
            mock_logger.exception.assert_not_called()
            mock_logger.error.assert_not_called()


class TestMemoryToolRegistration:
    """Test that the memory tool is properly registered in the metadata registry."""

    def test_memory_in_tool_metadata(self) -> None:
        """Test that memory tool appears in the metadata registry."""
        assert "memory" in builtin_tool_registry()
        meta = builtin_tool_registry().entries["memory"]
        assert meta.display_name == "Agent Memory"
        assert meta.status.value == "available"
        assert meta.setup_type.value == "none"
        assert meta.category.value == "productivity"
