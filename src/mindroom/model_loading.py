"""Shared model-loading helpers used across AI and agent construction."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, cast

from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter
from agno.models.vertexai.claude import Claude as VertexAIClaude

from mindroom.codex_model import CodexResponses, normalize_codex_model_id
from mindroom.constants import RuntimePaths, runtime_env_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.logging_config import get_logger
from mindroom.vertex_claude_prompt_cache import install_vertex_claude_prompt_cache_hook

if TYPE_CHECKING:
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig

logger = get_logger(__name__)

__all__ = ["get_model_instance"]


def _canonical_provider(provider: str) -> str:
    """Return normalized provider key for model dispatch."""
    return provider.strip().lower().replace("-", "_")


def _create_model_for_provider(  # noqa: C901, PLR0912
    provider: str,
    model_id: str,
    model_config: ModelConfig,
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
) -> Model:
    """Create a model instance for one provider."""
    canonical_provider = _canonical_provider(provider)

    if (
        canonical_provider not in {"ollama", "vertexai_claude", "codex", "openai_codex"}
        and "api_key" not in extra_kwargs
    ):
        api_key = get_api_key_for_provider(canonical_provider, runtime_paths=runtime_paths)
        if api_key:
            extra_kwargs["api_key"] = api_key

    if canonical_provider == "vertexai_claude":
        if "project_id" not in extra_kwargs:
            project_id = runtime_paths.env_value("ANTHROPIC_VERTEX_PROJECT_ID")
            if project_id:
                extra_kwargs["project_id"] = project_id
        if "region" not in extra_kwargs:
            region = runtime_paths.env_value("CLOUD_ML_REGION")
            if region:
                extra_kwargs["region"] = region
        if "base_url" not in extra_kwargs:
            base_url = runtime_paths.env_value("ANTHROPIC_VERTEX_BASE_URL")
            if base_url:
                extra_kwargs["base_url"] = base_url
        client_params = dict(cast("dict[str, Any]", extra_kwargs.get("client_params") or {}))
        if "credentials" not in client_params and (
            google_application_credentials := runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
        ):
            google_auth = importlib.import_module("google.auth")
            load_credentials_from_file = google_auth.load_credentials_from_file
            credentials, _project_id = load_credentials_from_file(
                str(google_application_credentials),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client_params["credentials"] = credentials
        if client_params:
            extra_kwargs["client_params"] = client_params

    if canonical_provider in {"anthropic", "vertexai_claude"}:
        extra_kwargs.setdefault("cache_system_prompt", True)
        extra_kwargs.setdefault("extended_cache_time", True)

    if canonical_provider == "ollama":
        host = model_config.host or get_ollama_host(runtime_paths=runtime_paths) or "http://localhost:11434"
        logger.debug("using_ollama_host", host=host)
        return Ollama(id=model_id, host=host, **extra_kwargs)

    if canonical_provider == "openrouter":
        api_key = extra_kwargs.pop("api_key", None)
        if not api_key:
            api_key = get_api_key_for_provider(canonical_provider, runtime_paths=runtime_paths)
        if not api_key:
            logger.warning("No OpenRouter API key found in environment or CredentialsManager")
        return OpenRouter(id=model_id, api_key=api_key, **extra_kwargs)

    if canonical_provider in {"codex", "openai_codex"}:
        extra_kwargs.pop("api_key", None)
        return CodexResponses(id=normalize_codex_model_id(model_id), **extra_kwargs)

    provider_map: dict[str, type[Any]] = {
        "openai": OpenAIChat,
        "anthropic": Claude,
        "gemini": Gemini,
        "google": Gemini,
        "vertexai_claude": VertexAIClaude,
        "cerebras": Cerebras,
        "groq": Groq,
        "deepseek": DeepSeek,
    }

    model_class = provider_map.get(canonical_provider)
    if model_class is not None:
        return model_class(id=model_id, **extra_kwargs)

    msg = f"Unsupported AI provider: {provider}"
    raise ValueError(msg)


def get_model_instance(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str = "default",
) -> Model:
    """Get a model instance from config.yaml."""
    if model_name not in config.models:
        available = ", ".join(sorted(config.models.keys()))
        msg = f"Unknown model: {model_name}. Available models: {available}"
        raise ValueError(msg)

    model_config = config.models[model_name]
    provider = model_config.provider
    model_id = model_config.id

    logger.info("Using AI model", model=model_name, provider=provider, id=model_id)

    extra_kwargs = dict(model_config.extra_kwargs or {})

    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    model_creds = creds_manager.load_credentials(f"model:{model_name}")
    model_api_key = model_creds.get("api_key") if model_creds else None

    if model_api_key:
        extra_kwargs["api_key"] = model_api_key

    model = _create_model_for_provider(
        provider,
        model_id,
        model_config,
        extra_kwargs,
        runtime_paths,
    )
    if config.debug.log_llm_requests:
        install_llm_request_logging(
            model,
            agent_name=model_name,
            debug_config=config.debug,
            default_log_dir=runtime_paths.storage_root / "logs" / "llm_requests",
        )
    install_vertex_claude_prompt_cache_hook(model)
    return model
