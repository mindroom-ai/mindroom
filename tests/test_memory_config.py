"""Tests for memory configuration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, _MemoryEmbedderConfig, _MemoryLLMConfig
from mindroom.config.models import EmbedderConfig, RouterConfig
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.memory.config import _get_memory_config, _memory_collection_name, create_memory_instance
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import orchestrator_runtime_paths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _openai_memory_connections(
    *,
    llm_connection_id: str = "openai/default",
    llm_service: str = "openai",
    embedder_connection_id: str = "openai/embeddings",
    embedder_service: str = "openai",
) -> dict[str, dict[str, str]]:
    return {
        llm_connection_id: {
            "provider": "openai",
            "service": llm_service,
            "auth_kind": "api_key",
        },
        embedder_connection_id: {
            "provider": "openai",
            "service": embedder_service,
            "auth_kind": "api_key",
        },
    }


class TestMemoryConfig:
    """Test memory configuration."""

    def test_get_memory_config_with_ollama(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with Ollama embedder."""
        # Create config with Ollama embedder
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(
                model="nomic-embed-text",
                host="http://localhost:11434",
            ),
        )
        llm_config = _MemoryLLMConfig(
            provider="ollama",
            config={
                "model": "llama3.2",
                "host": "http://localhost:11434",
                "temperature": 0.1,
                "top_p": 1,
            },
        )
        memory = MemoryConfig(embedder=embedder_config, llm=llm_config)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, _runtime_paths(tmp_path))

        # Verify embedder config
        assert result["embedder"]["provider"] == "ollama"
        assert result["embedder"]["config"]["model"] == "nomic-embed-text"
        assert result["embedder"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify LLM config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == "llama3.2"
        assert result["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify vector store config
        assert result["vector_store"]["provider"] == "chroma"
        assert result["vector_store"]["config"]["collection_name"] == _memory_collection_name(config)
        assert str(storage_path / "chroma") in result["vector_store"]["config"]["path"]

    def test_get_memory_config_with_openai(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with OpenAI embedder."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).set_api_key("openai", "test-key")

        # Create config with OpenAI embedder
        embedder_config = _MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(model="text-embedding-ada-002"),
        )
        llm_config = _MemoryLLMConfig(
            provider="openai",
            config={"model": "gpt-4", "temperature": 0.1, "top_p": 1},
        )
        memory = MemoryConfig(embedder=embedder_config, llm=llm_config)
        config = Config(
            memory=memory,
            router=RouterConfig(model="default"),
            connections=_openai_memory_connections(),
        )

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, runtime_paths)

        # Verify embedder config
        assert result["embedder"]["provider"] == "openai"
        assert result["embedder"]["config"]["model"] == "text-embedding-ada-002"
        assert result["embedder"]["config"]["api_key"] == "test-key"

        # Verify LLM config
        assert result["llm"]["provider"] == "openai"
        assert result["llm"]["config"]["model"] == "gpt-4"
        assert result["llm"]["config"]["api_key"] == "test-key"

    def test_get_memory_config_passes_configured_embedding_dimensions(
        self,
        tmp_path: Path,
    ) -> None:
        """Configured embedding dimensions should be forwarded to Mem0."""
        embedder_config = _MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(
                model="gemini-embedding-001",
                host="http://example.com/v1",
                dimensions=3072,
            ),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        runtime_paths = _runtime_paths(tmp_path)
        config = Config(
            memory=memory,
            router=RouterConfig(model="default"),
            connections=_openai_memory_connections(),
        )
        get_runtime_shared_credentials_manager(runtime_paths).set_api_key("openai", "test-key")
        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["embedding_dims"] == 3072

    def test_get_memory_config_with_sentence_transformers(
        self,
        tmp_path: Path,
    ) -> None:
        """Sentence-transformers should map to Mem0's local huggingface embedder."""
        embedder_config = _MemoryEmbedderConfig(
            provider="sentence_transformers",
            config=EmbedderConfig(
                model="sentence-transformers/all-MiniLM-L6-v2",
                dimensions=384,
            ),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result["embedder"]["provider"] == "huggingface"
        assert result["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"
        assert result["embedder"]["config"]["embedding_dims"] == 384

    def test_get_memory_config_keeps_existing_huggingface_provider_support(
        self,
        tmp_path: Path,
    ) -> None:
        """Existing Mem0 providers should remain valid after adding sentence-transformers."""
        config = Config(
            memory={
                "embedder": {
                    "provider": "huggingface",
                    "config": {
                        "model": "sentence-transformers/all-MiniLM-L6-v2",
                    },
                },
            },
            router=RouterConfig(model="default"),
        )

        result = _get_memory_config(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result["embedder"]["provider"] == "huggingface"
        assert result["embedder"]["config"]["model"] == "sentence-transformers/all-MiniLM-L6-v2"

    def test_memory_collection_name_changes_when_embedder_changes(self) -> None:
        """Different embedder settings should isolate memories into different collections."""
        openai_memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model="text-embedding-3-small"),
            ),
            llm=None,
        )
        local_memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="sentence_transformers",
                config=EmbedderConfig(model="sentence-transformers/all-MiniLM-L6-v2"),
            ),
            llm=None,
        )
        openai_config = Config(memory=openai_memory, router=RouterConfig(model="default"))
        local_config = Config(memory=local_memory, router=RouterConfig(model="default"))

        assert _memory_collection_name(openai_config) != _memory_collection_name(local_config)

    def test_get_memory_config_uses_runtime_shared_credentials_path(self, tmp_path: Path) -> None:
        """Runtime-shared credential overrides should be visible to Mem0 provider config."""
        runtime_paths = resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env={"MINDROOM_SHARED_CREDENTIALS_PATH": str(tmp_path / ".shared_credentials")},
        )
        get_runtime_shared_credentials_manager(runtime_paths).set_api_key("openai", "shared-openai-key")

        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {"model": "text-embedding-3-small"},
                },
            },
            router=RouterConfig(model="default"),
            connections=_openai_memory_connections(),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["api_key"] == "shared-openai-key"

    def test_get_memory_config_works_with_synthesized_default_embedder_connection(self, tmp_path: Path) -> None:
        """The default Mem0 embedder should validate and resolve even when only the model connection is authored."""
        runtime_paths = _runtime_paths(tmp_path)
        get_runtime_shared_credentials_manager(runtime_paths).set_api_key("openai", "test-key")
        config = Config.validate_with_runtime(
            {
                "models": {
                    "default": {
                        "provider": "openai",
                        "id": "gpt-5.4",
                    },
                },
                "connections": {
                    "openai/default": {
                        "provider": "openai",
                        "service": "openai",
                        "auth_kind": "api_key",
                    },
                },
                "router": {"model": "default"},
            },
            runtime_paths,
            strict_connection_validation=True,
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["provider"] == "openai"
        assert result["embedder"]["config"]["model"] == "text-embedding-3-small"
        assert result["embedder"]["config"]["api_key"] == "test-key"

    def test_get_memory_config_uses_named_openai_connections(self, tmp_path: Path) -> None:
        """Memory config should honor explicitly named OpenAI connections for both consumers."""
        runtime_paths = _runtime_paths(tmp_path)
        credentials = get_runtime_shared_credentials_manager(runtime_paths)
        credentials.save_credentials("openai-memory-llm", {"api_key": "llm-key", "_source": "test"})
        credentials.save_credentials("openai-memory-embedder", {"api_key": "embed-key", "_source": "test"})

        config = Config(
            memory={
                "embedder": {
                    "provider": "openai",
                    "config": {
                        "model": "text-embedding-3-small",
                        "connection": "openai/memory-embedder",
                    },
                },
                "llm": {
                    "provider": "openai",
                    "connection": "openai/memory-llm",
                    "config": {
                        "model": "gpt-4o-mini",
                    },
                },
            },
            router=RouterConfig(model="default"),
            connections=_openai_memory_connections(
                llm_connection_id="openai/memory-llm",
                llm_service="openai-memory-llm",
                embedder_connection_id="openai/memory-embedder",
                embedder_service="openai-memory-embedder",
            ),
        )

        result = _get_memory_config(tmp_path / "memory", config, runtime_paths)

        assert result["embedder"]["config"]["api_key"] == "embed-key"
        assert result["llm"]["config"]["api_key"] == "llm-key"

    @pytest.mark.parametrize(
        ("model", "effective_dimensions"),
        [
            ("text-embedding-3-small", 1536),
            ("text-embedding-3-large", 1536),
        ],
    )
    def test_memory_collection_name_ignores_equivalent_mem0_openai_default_dimensions(
        self,
        model: str,
        effective_dimensions: int,
    ) -> None:
        """Equivalent Mem0 OpenAI defaults should reuse the same memory collection."""
        implicit_default = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model=model),
            ),
            llm=None,
        )
        explicit_default = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="openai",
                config=EmbedderConfig(model=model, dimensions=effective_dimensions),
            ),
            llm=None,
        )
        implicit_config = Config(memory=implicit_default, router=RouterConfig(model="default"))
        explicit_config = Config(memory=explicit_default, router=RouterConfig(model="default"))

        assert _memory_collection_name(implicit_config) == _memory_collection_name(explicit_config)

    def test_get_memory_config_no_model_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """Test memory config falls back to Ollama when no model configured."""
        # Create config with no models
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="nomic-embed-text", host=None),
        )
        # No memory.llm configured - should trigger fallback
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = _get_memory_config(storage_path, config, _runtime_paths(tmp_path))

        # Verify LLM fallback config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == "llama3.2"
        assert result["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

    def test_chroma_directory_creation(
        self,
        tmp_path: Path,
    ) -> None:
        """Test that ChromaDB directory is created."""
        # Create minimal config
        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Get config
        result = _get_memory_config(tmp_path, config, _runtime_paths(tmp_path))

        # Verify chroma path in config
        chroma_path = tmp_path / "chroma"
        assert str(chroma_path) == result["vector_store"]["config"]["path"]

        # Verify directory was created
        assert chroma_path.exists()
        assert chroma_path.is_dir()

    def test_relative_storage_path_remains_stable_after_cwd_change(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative storage paths should be anchored once and survive later cwd changes."""
        project_root = tmp_path / "project"
        project_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(project_root)

        orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(Path("mindroom_data")))

        other_cwd = tmp_path / "other"
        other_cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(other_cwd)

        embedder_config = _MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = _get_memory_config(orchestrator.storage_path, config, orchestrator.runtime_paths)

        expected_storage = (project_root / "mindroom_data").resolve()
        expected_chroma = (expected_storage / "chroma").resolve()
        assert orchestrator.storage_path == expected_storage
        assert Path(result["vector_store"]["config"]["path"]) == expected_chroma

    @pytest.mark.asyncio
    @patch("mindroom.memory.config.ensure_sentence_transformers_dependencies")
    @patch("mindroom.memory.config.AsyncMemory.from_config", new_callable=AsyncMock)
    async def test_create_memory_instance_auto_installs_sentence_transformers(
        self,
        mock_from_config: AsyncMock,
        mock_ensure_sentence_transformers_dependencies: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Creating a local-embedder Mem0 instance should trigger optional runtime install."""
        memory = MemoryConfig(
            embedder=_MemoryEmbedderConfig(
                provider="sentence_transformers",
                config=EmbedderConfig(model="sentence-transformers/all-MiniLM-L6-v2"),
            ),
            llm=None,
        )
        config = Config(memory=memory, router=RouterConfig(model="default"))
        expected_memory = object()
        mock_from_config.return_value = expected_memory

        result = await create_memory_instance(tmp_path / "memory", config, _runtime_paths(tmp_path))

        assert result is expected_memory
        mock_ensure_sentence_transformers_dependencies.assert_called_once_with(_runtime_paths(tmp_path))
        mock_from_config.assert_awaited_once()

    def test_memory_auto_flush_batch_config_is_parameterized(self) -> None:
        """Auto-flush batch/extractor limits should be configurable."""
        memory = MemoryConfig.model_validate(
            {
                "backend": "file",
                "team_reads_member_memory": True,
                "auto_flush": {
                    "enabled": True,
                    "batch": {
                        "max_sessions_per_cycle": 7,
                        "max_sessions_per_agent_per_cycle": 2,
                    },
                    "extractor": {
                        "max_messages_per_flush": 12,
                        "max_chars_per_flush": 9000,
                    },
                },
            },
        )
        assert memory.backend == "file"
        assert memory.team_reads_member_memory is True
        assert memory.auto_flush.enabled is True
        assert memory.auto_flush.batch.max_sessions_per_cycle == 7
        assert memory.auto_flush.batch.max_sessions_per_agent_per_cycle == 2
        assert memory.auto_flush.extractor.max_messages_per_flush == 12
        assert memory.auto_flush.extractor.max_chars_per_flush == 9000

    def test_memory_auto_flush_default_interval_is_30_minutes(self) -> None:
        """Auto-flush should default to a half-hour worker interval."""
        memory = MemoryConfig()
        assert memory.auto_flush.flush_interval_seconds == 1800
