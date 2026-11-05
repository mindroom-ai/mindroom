"""Embedder construction shared by knowledge and memory semantic indexes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.constants import runtime_env_path
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_embedder_api_key, get_ollama_api_key, get_ollama_host, get_openai_base_url
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder
from mindroom.embeddings.sentence_transformers_embedder import create_sentence_transformers_embedder
from mindroom.providers.google_adc import load_google_application_credentials

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder
    from mindroom.embeddings.embedder_health import EmbedderHealthRecorder


@dataclass(frozen=True, slots=True)
class _ResolvedEmbedderSettings:
    """Credential-resolved settings for one semantic embedder client."""

    provider: str
    model: str
    host: str | None
    dimensions: int | None
    query_prefix: str | None = None
    document_prefix: str | None = None
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
                "query_prefix": self.query_prefix,
                "document_prefix": self.document_prefix,
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
            host=embedder_config.host or get_openai_base_url(runtime_paths),
            dimensions=embedder_config.dimensions,
            query_prefix=embedder_config.query_prefix,
            document_prefix=embedder_config.document_prefix,
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
            host=get_ollama_host(runtime_paths=runtime_paths, configured_host=embedder_config.host, saved_first=True),
            dimensions=None,
            api_key=get_ollama_api_key(runtime_paths),
        )
    if provider == "vertex_ai":
        return _ResolvedEmbedderSettings(
            provider=provider,
            model=embedder_config.model,
            host=None,
            dimensions=embedder_config.dimensions,
            query_prefix=embedder_config.query_prefix,
            document_prefix=embedder_config.document_prefix,
        )
    return _ResolvedEmbedderSettings(
        provider=provider,
        model=embedder_config.model,
        host=None,
        dimensions=embedder_config.dimensions,
    )


def create_configured_embedder(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    health_recorder: EmbedderHealthRecorder | None = None,
    caller_retries: bool = False,
) -> Embedder:
    """Create the configured embedding provider used for semantic indexes.

    ``caller_retries`` turns off the OpenAI embedder's own retries for a caller that retries failed requests itself, as
    a knowledge refresh does, so its classified failures alone decide what is sent again. Other callers keep those
    retries, since nothing retries their requests in place: the knowledge graph retries only a whole episode minutes
    later. The Vertex AI embedder keeps its retries for every caller: it sends one request per text, and a refresh
    retries a whole file. The Ollama and local embedders do not retry on their own.
    """
    settings = resolve_embedder_settings(config, runtime_paths)

    # Provider SDK modules are imported at first construction so only the configured provider's SDK loads (#1436).
    if settings.provider == "openai":
        from mindroom.embeddings.openai_embedder import OpenAIEmbedder  # noqa: PLC0415

        return OpenAIEmbedder(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.host,
            dimensions=settings.dimensions,
            query_prefix=settings.query_prefix,
            document_prefix=settings.document_prefix,
            max_retries=0 if caller_retries else None,
            health_recorder=capture_embedder_health_recorder() if health_recorder is None else health_recorder,
        )

    if settings.provider == "ollama":
        from mindroom.embeddings.ollama_embedder import OllamaEmbedder  # noqa: PLC0415

        # The resolver always resolves an Ollama host, as it does an OpenAI endpoint.
        assert settings.host is not None
        return OllamaEmbedder(model=settings.model, host=settings.host, api_key=settings.api_key)

    if settings.provider == "vertex_ai":
        from mindroom.embeddings.vertex_ai_embedder import VertexAIEmbedder  # noqa: PLC0415

        credentials_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
        # Without the variable, google-genai finds Application Default Credentials, such as Workload Identity, and
        # their project itself.
        credentials = load_google_application_credentials(str(credentials_path)) if credentials_path else None
        return VertexAIEmbedder(
            model=settings.model,
            health_recorder=capture_embedder_health_recorder() if health_recorder is None else health_recorder,
            # A service account key names its own project; user credentials name none.
            project=get_secret_from_env("GOOGLE_CLOUD_PROJECT", runtime_paths=runtime_paths)
            or getattr(credentials, "project_id", None),
            # Gemini embedding models serve at global. A location also keeps google-genai from signing requests with
            # a Gemini API key from the environment instead of these credentials.
            location=get_secret_from_env("GOOGLE_CLOUD_LOCATION", runtime_paths=runtime_paths) or "global",
            credentials=credentials,
            dimensions=settings.dimensions,
            query_prefix=settings.query_prefix,
            document_prefix=settings.document_prefix,
        )

    if settings.provider == "sentence_transformers":
        return create_sentence_transformers_embedder(runtime_paths, settings.model)

    msg = (
        f"Unsupported semantic-search embedder provider: {settings.provider}. "
        "Supported providers: openai, ollama, sentence_transformers, vertex_ai"
    )
    raise ValueError(msg)


def embedder_client_signature(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return a non-secret fingerprint of the concrete runtime client."""
    return resolve_embedder_settings(config, runtime_paths).client_signature
