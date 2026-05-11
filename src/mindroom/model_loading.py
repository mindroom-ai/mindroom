"""Shared model-loading helpers used across AI and agent construction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter

from mindroom.codex_model import CodexResponses, derive_codex_prompt_cache_key, normalize_codex_model_id
from mindroom.connections import (
    connection_api_key,
    connection_google_application_credentials_path,
    resolve_connection,
)
from mindroom.constants import RuntimePaths, runtime_env_path
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.google_adc import load_google_application_credentials
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.logging_config import get_logger
from mindroom.runtime_env_policy import VERTEXAI_CLAUDE_ENV_BY_KEY
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from mindroom.vertex_claude_prompt_cache import install_vertex_claude_prompt_cache_hook

if TYPE_CHECKING:
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = ["get_model_instance"]


def _canonical_provider(provider: str) -> str:
    """Return normalized provider key for model dispatch."""
    return provider.strip().lower().replace("-", "_")


def _create_model_for_provider(  # noqa: C901, PLR0912, PLR0915
    config: Config,
    provider: str,
    model_id: str,
    model_config: ModelConfig,
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> Model:
    """Create a model instance for one provider."""
    canonical_provider = _canonical_provider(provider)
    supported_providers = {
        "openai",
        "anthropic",
        "gemini",
        "google",
        "vertexai_claude",
        "cerebras",
        "groq",
        "deepseek",
        "ollama",
        "openrouter",
        "codex",
        "openai_codex",
    }
    if canonical_provider not in supported_providers:
        msg = f"Unsupported AI provider: {provider}"
        raise ValueError(msg)

    resolved_connection = None
    if canonical_provider not in {"codex", "openai_codex"}:
        resolved_connection = resolve_connection(
            config,
            provider=provider,
            purpose="chat_model",
            connection_name=model_config.connection,
            runtime_paths=runtime_paths,
            validate_credentials=False,
        )
        if canonical_provider not in {"ollama", "vertexai_claude"}:
            api_key = connection_api_key(resolved_connection) or get_api_key_for_provider(
                canonical_provider,
                runtime_paths=runtime_paths,
            )
            if api_key:
                extra_kwargs["api_key"] = api_key

    if canonical_provider == "vertexai_claude":
        if "project_id" not in extra_kwargs:
            project_id = runtime_paths.env_value(VERTEXAI_CLAUDE_ENV_BY_KEY["project_id"])
            if project_id:
                extra_kwargs["project_id"] = project_id
        if "region" not in extra_kwargs:
            region = runtime_paths.env_value(VERTEXAI_CLAUDE_ENV_BY_KEY["region"])
            if region:
                extra_kwargs["region"] = region
        if "base_url" not in extra_kwargs:
            base_url = runtime_paths.env_value("ANTHROPIC_VERTEX_BASE_URL")
            if base_url:
                extra_kwargs["base_url"] = base_url
        client_params = dict(cast("dict[str, Any]", extra_kwargs.get("client_params") or {}))
        google_application_credentials = (
            connection_google_application_credentials_path(resolved_connection)
            if resolved_connection is not None
            else None
        )
        if google_application_credentials is None:
            adc_path = runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
            if adc_path is not None:
                google_application_credentials = str(adc_path)
        if "credentials" not in client_params and google_application_credentials:
            client_params["credentials"] = load_google_application_credentials(google_application_credentials)
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
        if "prompt_cache_key" not in extra_kwargs and execution_identity is not None:
            prompt_cache_key = derive_codex_prompt_cache_key(execution_identity)
            if prompt_cache_key is not None:
                extra_kwargs["prompt_cache_key"] = prompt_cache_key
        return CodexResponses(id=normalize_codex_model_id(model_id), **extra_kwargs)

    provider_map: dict[str, type[Any]] = {
        "openai": OpenAIChat,
        "anthropic": Claude,
        "gemini": Gemini,
        "google": Gemini,
        "vertexai_claude": MindroomVertexAIClaude,
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
    execution_identity: ToolExecutionIdentity | None = None,
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

    if _canonical_provider(provider) in {"codex", "openai_codex"}:
        extra_kwargs.setdefault("default_instructions", config.get_prompt("CODEX_DEFAULT_INSTRUCTIONS"))

    model = _create_model_for_provider(
        config,
        provider,
        model_id,
        model_config,
        extra_kwargs,
        runtime_paths,
        execution_identity,
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
