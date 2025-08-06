"""Tests for memory configuration."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from mindroom.memory.config import get_memory_config


class TestMemoryConfig:
    """Test memory configuration."""

    @patch("mindroom.memory.config.load_config")
    def test_get_memory_config_with_ollama(self, mock_load_config):
        """Test memory config creation with Ollama embedder."""
        # Mock config with Ollama embedder
        mock_config = MagicMock()
        mock_config.memory.embedder.provider = "ollama"
        mock_config.memory.embedder.config.model = "nomic-embed-text"
        mock_config.memory.embedder.config.host = "http://localhost:11434"

        # Mock default model
        mock_model = MagicMock()
        mock_model.provider = "ollama"
        mock_model.id = "llama3.2"
        mock_model.host = "http://localhost:11434"
        mock_config.models.get.return_value = mock_model

        mock_load_config.return_value = mock_config

        # Test config generation
        storage_path = Path("/tmp/test_memory")
        config = get_memory_config(storage_path)

        # Verify embedder config
        assert config["embedder"]["provider"] == "ollama"
        assert config["embedder"]["config"]["model"] == "nomic-embed-text"
        assert config["embedder"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify LLM config
        assert config["llm"]["provider"] == "ollama"
        assert config["llm"]["config"]["model"] == "llama3.2"
        assert config["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

        # Verify vector store config
        assert config["vector_store"]["provider"] == "chroma"
        assert config["vector_store"]["config"]["collection_name"] == "mindroom_memories"
        assert str(storage_path / "chroma") in config["vector_store"]["config"]["path"]

    @patch("mindroom.memory.config.load_config")
    @patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"})
    def test_get_memory_config_with_openai(self, mock_load_config):
        """Test memory config creation with OpenAI embedder."""
        # Mock config with OpenAI embedder
        mock_config = MagicMock()
        mock_config.memory.embedder.provider = "openai"
        mock_config.memory.embedder.config.model = "text-embedding-ada-002"

        # Mock OpenAI model
        mock_model = MagicMock()
        mock_model.provider = "openai"
        mock_model.id = "gpt-4"
        mock_model.host = None
        mock_config.models.get.return_value = mock_model

        mock_load_config.return_value = mock_config

        # Test config generation
        storage_path = Path("/tmp/test_memory")
        config = get_memory_config(storage_path)

        # Verify embedder config
        assert config["embedder"]["provider"] == "openai"
        assert config["embedder"]["config"]["model"] == "text-embedding-ada-002"
        assert config["embedder"]["config"]["api_key"] == "test-key"

        # Verify LLM config
        assert config["llm"]["provider"] == "openai"
        assert config["llm"]["config"]["model"] == "gpt-4"
        assert config["llm"]["config"]["api_key"] == "test-key"

    @patch("mindroom.memory.config.load_config")
    @patch.dict("os.environ", {}, clear=True)
    def test_get_memory_config_no_model_fallback(self, mock_load_config):
        """Test memory config falls back to Ollama when no model configured."""
        # Mock config with no models
        mock_config = MagicMock()
        mock_config.memory.embedder.provider = "ollama"
        mock_config.memory.embedder.config.model = "nomic-embed-text"
        mock_config.memory.embedder.config.host = None
        mock_config.models.get.return_value = None

        mock_load_config.return_value = mock_config

        # Test config generation
        storage_path = Path("/tmp/test_memory")
        config = get_memory_config(storage_path)

        # Verify LLM fallback config
        assert config["llm"]["provider"] == "ollama"
        assert config["llm"]["config"]["model"] == "llama3.2"
        assert config["llm"]["config"]["ollama_base_url"] == "http://localhost:11434"

    def test_chroma_directory_creation(self, tmp_path):
        """Test that ChromaDB directory is created."""
        with patch("mindroom.memory.config.load_config") as mock_load_config:
            # Mock minimal config
            mock_config = MagicMock()
            mock_config.memory.embedder.provider = "ollama"
            mock_config.memory.embedder.config.model = "test"
            mock_config.memory.embedder.config.host = None
            mock_config.models.get.return_value = None
            mock_load_config.return_value = mock_config

            # Get config
            config = get_memory_config(tmp_path)

            # Verify chroma path in config
            chroma_path = tmp_path / "chroma"
            assert str(chroma_path) == config["vector_store"]["config"]["path"]

            # Verify directory was created
            assert chroma_path.exists()
            assert chroma_path.is_dir()
