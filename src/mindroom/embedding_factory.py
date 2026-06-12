"""Embedder construction shared by knowledge and memory semantic indexes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.knowledge.embedder.ollama import OllamaEmbedder

from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.embeddings import MindRoomOpenAIEmbedder, create_sentence_transformers_embedder
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def create_configured_embedder(config: Config, runtime_paths: RuntimePaths) -> Embedder:
    """Create the configured embedding provider used for semantic indexes."""
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config

    if provider == "openai":
        return MindRoomOpenAIEmbedder(
            id=embedder_config.model,
            api_key=get_api_key_for_provider("openai", runtime_paths=runtime_paths),
            base_url=embedder_config.host,
            dimensions=embedder_config.dimensions,
        )

    if provider == "ollama":
        host = get_ollama_host(runtime_paths=runtime_paths) or embedder_config.host or OLLAMA_HOST_DEFAULT
        return OllamaEmbedder(id=embedder_config.model, host=host)

    if provider == "sentence_transformers":
        return create_sentence_transformers_embedder(
            runtime_paths,
            embedder_config.model,
            dimensions=embedder_config.dimensions,
        )

    msg = (
        f"Unsupported semantic-search embedder provider: {provider}. "
        "Supported providers: openai, ollama, sentence_transformers"
    )
    raise ValueError(msg)
