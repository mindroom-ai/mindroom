"""Tests for memory configuration."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from mindroom.config import (
    Config,
    EmbedderConfig,
    MemoryConfig,
    MemoryEmbedderConfig,
    MemoryLLMConfig,
    RouterConfig,
)
from mindroom.memory.config import get_memory_config

from .conftest import TEST_MEMORY_DIR


class TestMemoryConfig:
    """Test memory configuration."""

    def test_get_memory_config_with_ollama(self) -> None:
        """Test memory config creation with Ollama embedder."""
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
        storage_path = Path(TEST_MEMORY_DIR)
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

    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_get_memory_config_with_openai(self) -> None:
        """Test memory config creation with OpenAI embedder."""
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
        storage_path = Path(TEST_MEMORY_DIR)
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

    @patch.dict("os.environ", {}, clear=True)
    def test_get_memory_config_no_model_fallback(self) -> None:
        """Test memory config falls back to Ollama when no model configured."""
        # Create config with no models
        embedder_config = MemoryEmbedderConfig(
            provider="ollama",
            config=EmbedderConfig(model="nomic-embed-text", host=None),
        )
        # No memory.llm configured - should trigger fallback
        memory = MemoryConfig(embedder=embedder_config, llm=None)
        config = Config(memory=memory, router=RouterConfig(model="default"))

        # Test config generation
        storage_path = Path(TEST_MEMORY_DIR)
        result = get_memory_config(storage_path, config)

        # Verify LLM fallback config
        assert result["llm"]["provider"] == "ollama"
        assert result["llm"]["config"]["model"] == "llama3.2"
        assert result["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

    def test_chroma_directory_creation(self, tmp_path: Path) -> None:
        """Test that ChromaDB directory is created."""
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
