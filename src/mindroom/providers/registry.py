"""Build the provider a configured model answers with: its family's adapter inside the request pipeline.

A key the dashboard saved for the model (``model:<name>``) beats the configured key, which beats the provider's
shared key; Codex gets the configured default instructions; and every request goes through the same idle limit,
media fallback prompt, and request log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from mindroom.logging_config import get_logger
from mindroom.providers.anthropic.settings import ClaudeRouteKind
from mindroom.providers.gemini.adapter import build_provider as build_gemini_provider
from mindroom.providers.model_loading import dashboard_api_key
from mindroom.providers.names import canonical_provider
from mindroom.providers.ollama_adapter import build_provider as build_ollama_provider
from mindroom.providers.openai.build import OPENAI_FAMILY_PROVIDERS, ProviderRuntime
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.pipeline.compose import stream_idle_timeout_seconds, wrap_provider
from mindroom.providers.pipeline.request_log import RequestLog
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import Capabilities, ModelProvider
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["build_model_provider", "model_capabilities"]

logger = get_logger(__name__)

_CLAUDE_PROVIDERS = frozenset(get_args(ClaudeRouteKind.__value__))
_GEMINI_PROVIDERS = frozenset({"google", "gemini"})


def _build_adapter(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
    codex_default_instructions: str,
) -> ModelProvider:
    """Return the bare adapter of the family that serves ``model_config.provider``."""
    provider = canonical_provider(model_config.provider)
    if provider in OPENAI_FAMILY_PROVIDERS:
        runtime = ProviderRuntime(
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            codex_default_instructions=codex_default_instructions,
        )
        return build_openai_provider(model_config, runtime)
    if provider in _CLAUDE_PROVIDERS:
        from mindroom.providers.anthropic.messages import (  # noqa: PLC0415 - keep the anthropic SDK out of import time
            build_provider as build_claude_provider,
        )

        return build_claude_provider(model_config, runtime_paths)
    if provider in _GEMINI_PROVIDERS:
        return build_gemini_provider(model_config, runtime_paths)
    if provider == "ollama":
        return build_ollama_provider(model_config, runtime_paths)
    if provider == "synthetic":
        return build_synthetic_provider(model_config, runtime_paths)
    msg = f"Unsupported AI provider: {model_config.provider}"
    raise ValueError(msg)


def build_model_provider(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str = "default",
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> ModelProvider:
    """Return the provider for the model configured as ``model_name``, with the full request pipeline.

    ``execution_identity`` scopes the prompt-cache and session keys of the Codex and Kimi routes.
    Raises ``ValueError`` for an unknown model name, an unsupported provider, or ``extra_kwargs`` its route
    does not accept.
    """
    if model_name not in config.models:
        available = ", ".join(sorted(config.models))
        msg = f"Unknown model: {model_name}. Available models: {available}"
        raise ValueError(msg)
    model_config = config.models[model_name]
    if dashboard_key := dashboard_api_key(model_name, runtime_paths):
        model_config = model_config.model_copy(update={"api_key": dashboard_key})
    adapter = _build_adapter(
        model_config,
        runtime_paths,
        execution_identity=execution_identity,
        codex_default_instructions=config.get_prompt("CODEX_DEFAULT_INSTRUCTIONS"),
    )
    logger.info(
        "Using AI model",
        model=model_name,
        provider=model_config.provider,
        configured_id=model_config.id,
        effective_id=adapter.identity.id,
    )
    request_log = RequestLog(
        model_name=model_name,
        configured_provider=model_config.provider,
        debug=config.debug,
        default_log_dir=runtime_paths.storage_root / "logs" / "llm_requests",
    )
    return wrap_provider(
        adapter,
        idle_timeout_seconds=stream_idle_timeout_seconds(model_config, runtime_paths),
        media_fallback_prompt=config.get_prompt("INLINE_MEDIA_FALLBACK_PROMPT"),
        request_log=request_log,
    )


def model_capabilities(config: Config, runtime_paths: RuntimePaths, model_name: str) -> Capabilities:
    """Return what the route of the model configured as ``model_name`` supports; building its adapter sends nothing."""
    adapter = _build_adapter(
        config.models[model_name],
        runtime_paths,
        execution_identity=None,
        codex_default_instructions=config.get_prompt("CODEX_DEFAULT_INSTRUCTIONS"),
    )
    return adapter.capabilities
