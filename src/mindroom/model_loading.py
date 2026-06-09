"""Shared model-loading helpers used across AI and agent construction."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from agno.models.anthropic import Claude
from agno.models.azure import AzureOpenAI
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.llama_cpp import LlamaCpp
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter

from mindroom.codex_model import CodexResponses, derive_codex_prompt_cache_key, normalize_codex_model_id
from mindroom.constants import RuntimePaths, runtime_env_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host, get_secret_from_env
from mindroom.google_adc import load_google_application_credentials
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.logging_config import get_logger
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT
from mindroom.runtime_env_policy import (
    AWS_BEDROCK_CLAUDE_ENV_BY_KEY,
    AZURE_OPENAI_ENV_BY_KEY,
    VERTEXAI_CLAUDE_ENV_BY_KEY,
)
from mindroom.tool_system.dependencies import ensure_optional_deps
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from mindroom.vertex_claude_prompt_cache import install_vertex_claude_prompt_cache_hook

if TYPE_CHECKING:
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = ["get_model_instance"]

_BEDROCK_CLAUDE_PROVIDER = "bedrock_claude"
# The anthropic SDK rejects non-streaming requests whose max_tokens project past
# 10 minutes unless the client has an explicit timeout; 3600s is the SDK's own
# ceiling for non-streaming operations.
_CLAUDE_REQUEST_TIMEOUT_SECONDS = 3600.0


def _canonical_provider(provider: str) -> str:
    """Return normalized provider key for model dispatch."""
    return provider.strip().lower().replace("-", "_")


def _populate_azure_openai_runtime_kwargs(
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
) -> None:
    """Populate Azure OpenAI client settings from the active runtime env."""
    if "api_key" not in extra_kwargs:
        api_key = get_secret_from_env(AZURE_OPENAI_ENV_BY_KEY["api_key"], runtime_paths=runtime_paths)
        if api_key:
            extra_kwargs["api_key"] = api_key
    if "azure_endpoint" not in extra_kwargs:
        azure_endpoint = get_secret_from_env(AZURE_OPENAI_ENV_BY_KEY["endpoint"], runtime_paths=runtime_paths)
        if azure_endpoint:
            extra_kwargs["azure_endpoint"] = azure_endpoint
    if "api_version" not in extra_kwargs:
        api_version = runtime_paths.env_value(AZURE_OPENAI_ENV_BY_KEY["api_version"])
        if api_version:
            extra_kwargs["api_version"] = api_version
    if "azure_deployment" not in extra_kwargs:
        azure_deployment = runtime_paths.env_value(AZURE_OPENAI_ENV_BY_KEY["deployment"])
        if azure_deployment:
            extra_kwargs["azure_deployment"] = azure_deployment


def _populate_bedrock_claude_runtime_kwargs(
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
) -> None:
    """Populate AWS Bedrock Claude client settings from the active runtime env."""
    explicit_profile = extra_kwargs.pop("aws_profile", None)
    if "session" in extra_kwargs:
        return

    if "aws_region" not in extra_kwargs:
        aws_region = get_secret_from_env(
            AWS_BEDROCK_CLAUDE_ENV_BY_KEY["region"],
            runtime_paths=runtime_paths,
        ) or get_secret_from_env(
            AWS_BEDROCK_CLAUDE_ENV_BY_KEY["default_region"],
            runtime_paths=runtime_paths,
        )
        if aws_region:
            extra_kwargs["aws_region"] = aws_region

    has_explicit_static_credentials = "aws_access_key" in extra_kwargs or "aws_secret_key" in extra_kwargs
    if has_explicit_static_credentials:
        _populate_bedrock_claude_static_credentials(extra_kwargs, runtime_paths)
        if explicit_profile:
            logger.debug("bedrock_claude_profile_ignored_with_static_credentials")
        return

    if explicit_profile:
        _set_bedrock_claude_session(extra_kwargs, str(explicit_profile))
        return

    _populate_bedrock_claude_static_credentials(extra_kwargs, runtime_paths)
    if extra_kwargs.get("aws_access_key") or extra_kwargs.get("aws_secret_key"):
        return

    env_profile = get_secret_from_env(
        AWS_BEDROCK_CLAUDE_ENV_BY_KEY["profile"],
        runtime_paths=runtime_paths,
    )
    _set_bedrock_claude_session(extra_kwargs, env_profile)


def _populate_bedrock_claude_static_credentials(
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
) -> None:
    """Populate Bedrock static credentials from standard AWS env names."""
    for config_key, env_key in (
        ("aws_access_key", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["access_key"]),
        ("aws_secret_key", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["secret_key"]),
        ("aws_session_token", AWS_BEDROCK_CLAUDE_ENV_BY_KEY["session_token"]),
    ):
        if config_key in extra_kwargs:
            continue
        value = get_secret_from_env(env_key, runtime_paths=runtime_paths)
        if value:
            extra_kwargs[config_key] = value


def _set_bedrock_claude_session(extra_kwargs: dict[str, Any], aws_profile: str | None) -> None:
    """Create a boto3 session for profile or ambient IAM-role credential resolution."""
    session_kwargs: dict[str, str] = {}
    if aws_profile:
        session_kwargs["profile_name"] = str(aws_profile)
    if aws_region := extra_kwargs.get("aws_region"):
        session_kwargs["region_name"] = str(aws_region)
    session_module = import_module("boto3.session")
    extra_kwargs["session"] = session_module.Session(**session_kwargs)


def _create_model_for_provider(  # noqa: C901, PLR0912, PLR0915
    provider: str,
    model_id: str,
    model_config: ModelConfig,
    extra_kwargs: dict[str, Any],
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> Model:
    """Create a model instance for one provider."""
    canonical_provider = _canonical_provider(provider)

    if (
        canonical_provider
        not in {"ollama", "llama_cpp", "vertexai_claude", "codex", "openai_codex", _BEDROCK_CLAUDE_PROVIDER}
        and "api_key" not in extra_kwargs
    ):
        api_key = get_api_key_for_provider(canonical_provider, runtime_paths=runtime_paths)
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
        if "credentials" not in client_params and (
            google_application_credentials := runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
        ):
            client_params["credentials"] = load_google_application_credentials(str(google_application_credentials))
        if client_params:
            extra_kwargs["client_params"] = client_params

    if canonical_provider == "azure":
        _populate_azure_openai_runtime_kwargs(extra_kwargs, runtime_paths)

    if canonical_provider in {"anthropic", "vertexai_claude", _BEDROCK_CLAUDE_PROVIDER}:
        extra_kwargs.setdefault("cache_system_prompt", True)
        extra_kwargs.setdefault("extended_cache_time", True)

    if canonical_provider in {"anthropic", "vertexai_claude"}:
        extra_kwargs.setdefault("timeout", _CLAUDE_REQUEST_TIMEOUT_SECONDS)

    if canonical_provider == "ollama":
        host = model_config.host or get_ollama_host(runtime_paths=runtime_paths) or OLLAMA_HOST_DEFAULT
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

    if canonical_provider == _BEDROCK_CLAUDE_PROVIDER:
        extra_kwargs.pop("api_key", None)
        ensure_optional_deps(
            ["boto3"],
            "aws_bedrock",
            runtime_paths,
            missing_message="Missing AWS Bedrock dependencies. Install with: pip install 'mindroom[aws_bedrock]'",
        )
        _populate_bedrock_claude_runtime_kwargs(extra_kwargs, runtime_paths)
        aws_bedrock_module = import_module("agno.models.aws.claude")
        aws_bedrock_claude = cast("type[Model]", aws_bedrock_module.Claude)
        return aws_bedrock_claude(id=model_id, **extra_kwargs)

    provider_map: dict[str, type[Any]] = {
        "openai": OpenAIChat,
        "azure": AzureOpenAI,
        "anthropic": Claude,
        "gemini": Gemini,
        "google": Gemini,
        "vertexai_claude": MindroomVertexAIClaude,
        "llama_cpp": LlamaCpp,
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

    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    model_creds = creds_manager.load_credentials(f"model:{model_name}")
    model_api_key = model_creds.get("api_key") if model_creds else None

    if model_api_key:
        extra_kwargs["api_key"] = model_api_key

    if _canonical_provider(provider) in {"codex", "openai_codex"}:
        extra_kwargs.setdefault("default_instructions", config.get_prompt("CODEX_DEFAULT_INSTRUCTIONS"))

    model = _create_model_for_provider(
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
