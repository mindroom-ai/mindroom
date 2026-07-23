"""Embedder construction shared by knowledge and memory semantic indexes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.credentials_sync import get_embedder_api_key, get_ollama_host
from mindroom.embeddings import create_sentence_transformers_embedder
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embedder_health import EmbedderHealthRecorder


@dataclass(frozen=True, slots=True)
class _ResolvedEmbedderSettings:
    """Credential-resolved settings for one semantic embedder client."""

    provider: str
    model: str
    host: str | None
    dimensions: int | None
    api_key: str | None = field(default=None, repr=False)

    @property
    def client_signature(self) -> str:
        """Return a non-secret cache identity for this concrete client."""
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self.model,
                "host": self.host,
                "dimensions": self.dimensions,
                "api_key": self.api_key,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


def resolve_embedder_settings(config: Config, runtime_paths: RuntimePaths) -> _ResolvedEmbedderSettings:
    """Resolve provider settings and credentials through one runtime boundary."""
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config
    if provider == "openai":
        return _ResolvedEmbedderSettings(
            provider=provider,
            model=embedder_config.model,
            host=embedder_config.host,
            dimensions=embedder_config.dimensions,
            api_key=get_embedder_api_key(
                runtime_paths,
                explicit_api_key=embedder_config.api_key,
                credentials_service=embedder_config.credentials_service,
            ),
        )
    if provider == "ollama":
        return _ResolvedEmbedderSettings(
            provider=provider,
            model=embedder_config.model,
            host=get_ollama_host(runtime_paths=runtime_paths) or embedder_config.host or OLLAMA_HOST_DEFAULT,
            dimensions=embedder_config.dimensions,
        )
    return _ResolvedEmbedderSettings(
        provider=provider,
        model=embedder_config.model,
        host=embedder_config.host,
        dimensions=embedder_config.dimensions,
    )


def create_configured_embedder(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    health_recorder: EmbedderHealthRecorder | None = None,
) -> Embedder:
    """Create the configured embedding provider used for semantic indexes."""
    settings = resolve_embedder_settings(config, runtime_paths)

    if settings.provider == "openai":
        # Imported at first construction so only the configured embedder
        # provider's SDK loads (#1436).
        from mindroom.openai_embedder import MindRoomOpenAIEmbedder  # noqa: PLC0415

        if health_recorder is None:
            return MindRoomOpenAIEmbedder(
                id=settings.model,
                api_key=settings.api_key,
                base_url=settings.host,
                dimensions=settings.dimensions,
            )
        return MindRoomOpenAIEmbedder(
            id=settings.model,
            api_key=settings.api_key,
            base_url=settings.host,
            dimensions=settings.dimensions,
            health_recorder=health_recorder,
        )

    if settings.provider == "ollama":
        from agno.knowledge.embedder.ollama import OllamaEmbedder  # noqa: PLC0415

        return OllamaEmbedder(id=settings.model, host=settings.host)

    if settings.provider == "sentence_transformers":
        return create_sentence_transformers_embedder(
            runtime_paths,
            settings.model,
            dimensions=settings.dimensions,
        )

    msg = (
        f"Unsupported semantic-search embedder provider: {settings.provider}. "
        "Supported providers: openai, ollama, sentence_transformers"
    )
    raise ValueError(msg)


def embedder_client_signature(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return a non-secret fingerprint of the concrete runtime client."""
    return resolve_embedder_settings(config, runtime_paths).client_signature
