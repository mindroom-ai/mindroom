"""Tests for memory configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from mindroom.bot import MultiAgentOrchestrator

if TYPE_CHECKING:
    import pytest
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig, MemoryEmbedderConfig, MemoryLLMConfig
from mindroom.config.models import EmbedderConfig, RouterConfig
from mindroom.memory.config import get_memory_config


class TestMemoryConfig:
    """Test memory configuration."""

    @patch("mindroom.memory.config.get_credentials_manager")
    def test_get_memory_config_with_ollama(
        self,
        mock_get_creds_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with Ollama embedder."""
        # Mock credentials manager to return None for ollama credentials
        mock_creds_manager = MagicMock()
        mock_creds_manager.load_credentials.return_value = None
        mock_get_creds_manager.return_value = mock_creds_manager

        # Create config with Ollama embedder
        embedder_config = MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(
                model="nomic-embed-text",
                host="http://localhost:11434",
            ),
        )
        llm_config = MemoryLLMConfig(
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
        result = get_memory_config(storage_path, config)

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
        assert result["vector_store"]["config"]["collection_name"] == "mindroom_memories"
        assert str(storage_path / "chroma") in result["vector_store"]["config"]["path"]

    @patch("mindroom.memory.config.get_credentials_manager")
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_get_memory_config_with_openai(
        self,
        mock_get_creds_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test memory config creation with OpenAI embedder."""
        # Mock credentials manager to return test API key
        mock_creds_manager = MagicMock()
        mock_creds_manager.get_api_key.return_value = "test-key"
        mock_get_creds_manager.return_value = mock_creds_manager

        # Create config with OpenAI embedder
        embedder_config = MemoryEmbedderConfig(
            provider="openai",
            config=EmbedderConfig(model="text-embedding-ada-002"),
        )
        llm_config = MemoryLLMConfig(
            provider="openai",
            config={"model": "gpt-4", "temperature": 0.1, "top_p": 1},
        )
        memory = MemoryConfig(embedder=embedder_config, llm=llm_config)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = get_memory_config(storage_path, config)

        # Verify embedder config
        assert result["embedder"]["provider"] == "openai"
        assert result["embedder"]["config"]["model"] == "text-embedding-ada-002"
        # API key is now set as environment variable, not in config

        # Verify LLM config
        assert result["llm"]["provider"] == "openai"
        assert result["llm"]["config"]["model"] == "gpt-4"
        # API key is now set as environment variable, not in config

        # Verify the environment variable was set
        assert os.environ.get("OPENAI_API_KEY") == "test-key"

    @patch("mindroom.memory.config.get_credentials_manager")
    @patch.dict("os.environ", {}, clear=True)
    def test_get_memory_config_no_model_fallback(
        self,
        mock_get_creds_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test memory config falls back to Ollama when no model configured."""
        # Mock credentials manager to return None for ollama credentials
        mock_creds_manager = MagicMock()
        mock_creds_manager.load_credentials.return_value = None
        mock_get_creds_manager.return_value = mock_creds_manager

        # Create config with no models
        embedder_config = MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="nomic-embed-text", host=None),
        )
        # No memory.llm configured - should trigger fallback
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = tmp_path / "memory"
        result = get_memory_config(storage_path, config)

        # Verify LLM fallback config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == "llama3.2"
        assert result["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

    @patch("mindroom.memory.config.get_credentials_manager")
    def test_chroma_directory_creation(
        self,
        mock_get_creds_manager: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that ChromaDB directory is created."""
        # Mock credentials manager to return None for ollama credentials
        mock_creds_manager = MagicMock()
        mock_creds_manager.load_credentials.return_value = None
        mock_get_creds_manager.return_value = mock_creds_manager

        # Create minimal config
        embedder_config = MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Get config
        result = get_memory_config(tmp_path, config)

        # Verify chroma path in config
        chroma_path = tmp_path / "chroma"
        assert str(chroma_path) == result["vector_store"]["config"]["path"]

        # Verify directory was created
        assert chroma_path.exists()
        assert chroma_path.is_dir()

    @patch("mindroom.memory.config.get_credentials_manager")
    def test_relative_storage_path_remains_stable_after_cwd_change(
        self,
        mock_get_creds_manager: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Relative storage paths should be anchored once and survive later cwd changes."""
        mock_creds_manager = MagicMock()
        mock_creds_manager.load_credentials.return_value = None
        mock_get_creds_manager.return_value = mock_creds_manager

        project_root = tmp_path / "project"
        project_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(project_root)

        orchestrator = MultiAgentOrchestrator(storage_path=Path("mindroom_data"))

        other_cwd = tmp_path / "other"
        other_cwd.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(other_cwd)

        embedder_config = MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="test", host=None),
        )
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        result = get_memory_config(orchestrator.storage_path, config)

        expected_storage = (project_root / "mindroom_data").resolve()
        expected_chroma = (expected_storage / "chroma").resolve()
        assert orchestrator.storage_path == expected_storage
        assert Path(result["vector_store"]["config"]["path"]) == expected_chroma

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
