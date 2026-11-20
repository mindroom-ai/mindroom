"""Build the provider a configured model answers with: its family's adapter inside the request pipeline.

A key the dashboard saved for the model (``model:<name>``) beats the configured key, which beats the provider's
shared key; Codex gets the configured default instructions; and every request goes through the model's idle
limit, the media fallback prompt, and the request log.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, cast, get_args

from mindroom.llm.errors import AuthenticationFailed
from mindroom.logging_config import get_logger
from mindroom.providers.anthropic.settings import ACCEPTED_KEYS as CLAUDE_ACCEPTED_KEYS
from mindroom.providers.anthropic.settings import ENDPOINT_VARIABLES as CLAUDE_ENDPOINT_VARIABLES
from mindroom.providers.anthropic.settings import ClaudeRouteKind
from mindroom.providers.credentials import (
    api_key_env_reference,
    claude_authored_credentials,
    model_with_dashboard_key,
    resolve_api_key_reference,
)
from mindroom.providers.extra_kwargs import reject_unsupported_extra_kwargs, settings_keys
from mindroom.providers.gemini.settings import GeminiSettings
from mindroom.providers.names import provider_route
from mindroom.providers.ollama.settings import OllamaSettings
from mindroom.providers.openai.build import ENDPOINT_VARIABLES as OPENAI_ENDPOINT_VARIABLES
from mindroom.providers.openai.build import OPENAI_FAMILY_PROVIDERS, ProviderRuntime
from mindroom.providers.openai.build import accepted_extra_kwargs as openai_accepted_extra_kwargs
from mindroom.providers.openai.build import build_provider as build_openai_provider
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.pipeline.request_log import RequestLog
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.providers.reasoning import apply_reasoning_level
from mindroom.providers.synthetic_adapter import SyntheticSettings
from mindroom.providers.synthetic_adapter import build_provider as build_synthetic_provider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent, TokenCounter
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "build_model_provider",
    "check_extra_kwargs",
    "configured_model",
    "model_token_counter",
    "stream_idle_timeout_seconds",
]

logger = get_logger(__name__)

_CLAUDE_PROVIDERS = frozenset(get_args(ClaudeRouteKind.__value__))
# A hosted API that sends no stream event for this long has stalled.
_DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 300.0
# Local servers can stay silent for minutes while a request queues or a model loads.
_LOCAL_STREAM_PROVIDERS = frozenset({"ollama", "llama_cpp"})
# The runtime env variable each route reads its endpoint from when its config sets none, as the builders declare it.
_ENDPOINT_VARIABLES: dict[str, str] = {**OPENAI_ENDPOINT_VARIABLES, **CLAUDE_ENDPOINT_VARIABLES}
# The ``extra_kwargs`` keys of each provider whose one settings type takes every key it declares.
_SETTINGS_KEYS = {
    "google": settings_keys(GeminiSettings),
    "ollama": settings_keys(OllamaSettings),
    "synthetic": settings_keys(SyntheticSettings),
}


def configured_model(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelConfig:
    """Return the model configured as ``model_name``, with its reasoning level and the dashboard's key applied.

    A key written as ``${NAME}`` becomes that runtime env variable's value, and stays as written when it is unset.
    """
    if model_name not in config.models:
        available = ", ".join(sorted(config.models))
        msg = f"Unknown model: {model_name}. Available models: {available}"
        raise ValueError(msg)
    model_config = model_with_dashboard_key(model_name, config.models[model_name], runtime_paths)
    return resolve_api_key_reference(apply_reasoning_level(model_config), runtime_paths)


def _build_adapter(
    model_config: ModelConfig,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None,
) -> ModelProvider:
    """Return the bare adapter of the family that serves ``model_config.provider``."""
    provider = provider_route(model_config.provider)
    if provider in OPENAI_FAMILY_PROVIDERS:
        runtime = ProviderRuntime(
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            codex_default_instructions=config.get_prompt("CODEX_DEFAULT_INSTRUCTIONS"),
        )
        return build_openai_provider(model_config, runtime)
    if provider in _CLAUDE_PROVIDERS:
        from mindroom.providers.anthropic.messages import (  # noqa: PLC0415 - keep the anthropic SDK out of import time
            build_provider as build_claude_provider,
        )

        return build_claude_provider(model_config, runtime_paths)
    if provider == "google":
        from mindroom.providers.gemini.adapter import (  # noqa: PLC0415 - keep the google-genai SDK out of import time
            build_provider as build_gemini_provider,
        )

        return build_gemini_provider(model_config, runtime_paths)
    if provider == "ollama":
        from mindroom.providers.ollama.adapter import (  # noqa: PLC0415 - keep the ollama SDK out of import time
            build_provider as build_ollama_provider,
        )

        return build_ollama_provider(model_config, runtime_paths)
    if provider == "synthetic":
        return build_synthetic_provider(model_config, runtime_paths)
    msg = f"Unsupported AI provider: {model_config.provider}"
    raise ValueError(msg)


def _unset_key_stage(inner: ModelProvider, _request: ModelRequest, *, variable: str) -> AsyncIterator[StreamEvent]:
    """Fail a request of a model whose key references an unset variable, so the reference is never sent as a key."""
    msg = f"{variable} not set. This model's api_key reads the {variable} environment variable."
    raise AuthenticationFailed(msg, provider=inner.identity.provider, status_code=401)


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
    model_config = configured_model(config, runtime_paths, model_name)
    adapter = _build_adapter(model_config, config, runtime_paths, execution_identity=execution_identity)
    # A reference ``configured_model`` left in place has no value, so it must not be sent as the key.
    if variable := api_key_env_reference(model_config):
        adapter = WrappedProvider(adapter, partial(_unset_key_stage, variable=variable))
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
        storage_root=runtime_paths.storage_root,
    )
    return wrap_provider(
        adapter,
        idle_timeout_seconds=stream_idle_timeout_seconds(model_config, runtime_paths),
        media_fallback_prompt=config.get_prompt("INLINE_MEDIA_FALLBACK_PROMPT"),
        request_log=request_log,
    )


def model_token_counter(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> TokenCounter:
    """Return how the model configured as ``model_name`` counts tokens."""
    return build_model_provider(config, runtime_paths, model_name, execution_identity=execution_identity).token_counter


def _sets_base_url(value: object) -> bool:
    """Return whether settings, at any depth, point the client at an explicit endpoint."""
    if not isinstance(value, dict):
        return False
    settings = cast("dict[str, Any]", value)
    return any((key == "base_url" and bool(item)) or _sets_base_url(item) for key, item in settings.items())


def stream_idle_timeout_seconds(model_config: ModelConfig, runtime_paths: RuntimePaths) -> float | None:
    """Return the silence limit for one model's streamed requests, or None for no limit.

    Unset, hosted providers on their built-in endpoint get 300 s; local servers and configured endpoints, a
    ``base_url`` anywhere in ``extra_kwargs`` or the endpoint variable the model's route reads, get no limit; ``0``
    disables it.
    """
    if model_config.stream_idle_timeout_seconds is not None:
        return model_config.stream_idle_timeout_seconds or None
    provider = provider_route(model_config.provider)
    if provider in _LOCAL_STREAM_PROVIDERS or _sets_base_url(model_config.extra_kwargs):
        return None
    if (variable := _ENDPOINT_VARIABLES.get(provider)) and runtime_paths.first_env_value((variable,)):
        return None
    return _DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS


def check_extra_kwargs(model_config: ModelConfig, runtime_paths: RuntimePaths) -> None:
    """Raise ``ValueError`` for an ``extra_kwargs`` key the route that serves ``model_config`` does not accept.

    A direct Claude model that authors more than one client credential fails too (``claude_authored_credentials``).

    Config load calls this so such a key fails before any model is built; it reads only the routes' key tables,
    so no provider SDK loads.
    """
    provider = provider_route(model_config.provider)
    if provider in OPENAI_FAMILY_PROVIDERS:
        allowed = openai_accepted_extra_kwargs(model_config, runtime_paths)
    elif provider in _CLAUDE_PROVIDERS:
        allowed = CLAUDE_ACCEPTED_KEYS[cast("ClaudeRouteKind", provider)]
    else:
        allowed = _SETTINGS_KEYS[provider]
    extra_kwargs = dict(model_config.extra_kwargs or {})
    # Each level writes the route's reasoning key, which the route must accept too.
    for level in model_config.reasoning.levels if model_config.reasoning else ():
        extra_kwargs.update(apply_reasoning_level(model_config.at_level(level)).extra_kwargs or {})
    reject_unsupported_extra_kwargs(extra_kwargs, allowed, provider=model_config.provider)
    if provider == "anthropic" and len(credentials := claude_authored_credentials(model_config)) > 1:
        msg = (
            f"sets {' and '.join(credentials)}, but the Anthropic client takes one credential; keep one of them."
        )
        raise ValueError(msg)
