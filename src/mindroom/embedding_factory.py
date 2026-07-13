"""Embedder construction shared by knowledge and memory semantic indexes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.credentials_sync import get_embedder_api_key, get_ollama_host
from mindroom.embeddings import create_sentence_transformers_embedder
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embedder_health import EmbedderHealthRecorder


def create_configured_embedder(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> Embedder:
    """Create the configured embedding provider used for semantic indexes."""
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config

    if provider == "openai":
        # Imported at first construction so only the configured embedder
        # provider's SDK loads (#1436).
        from mindroom.openai_embedder import MindRoomOpenAIEmbedder  # noqa: PLC0415

        api_key = get_embedder_api_key(runtime_paths, explicit_api_key=embedder_config.api_key)
        if health_recorder is None:
            return MindRoomOpenAIEmbedder(
                id=embedder_config.model,
                api_key=api_key,
                base_url=embedder_config.host,
                dimensions=embedder_config.dimensions,
            )
        return MindRoomOpenAIEmbedder(
            id=embedder_config.model,
            api_key=api_key,
            base_url=embedder_config.host,
            dimensions=embedder_config.dimensions,
            health_recorder=health_recorder,
        )

    if provider == "ollama":
        from agno.knowledge.embedder.ollama import OllamaEmbedder  # noqa: PLC0415

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
