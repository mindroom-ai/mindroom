"""Which key a configured model authenticates with: the dashboard key and missing-key checks.

The provider registry builds models with these rules; the CLI and the dashboard use them to report a model
that has no key before any request fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.models import split_model_reference
from mindroom.config.schema_hints import ENV_PLACEHOLDER_PATTERN
from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.providers.names import provider_route

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

__all__ = [
    "api_key_env_reference",
    "dashboard_api_key",
    "gemini_uses_vertexai",
    "missing_model_api_key_provider",
    "model_api_key",
    "model_uses_own_credential",
    "model_with_dashboard_key",
    "own_api_key",
    "resolve_api_key_reference",
    "unset_api_key_variable",
    "uses_alternative_auth",
]

# Settings the provider adapters accept in place of ``api_key``.
_ALTERNATIVE_AUTH_KWARGS: dict[str, tuple[str, ...]] = {
    "anthropic": ("auth_token",),
    "azure": ("azure_ad_token", "azure_ad_token_provider"),
}

# Routes that authenticate without an API key and ignore a configured one.
_KEYLESS_ROUTES = frozenset({"bedrock_claude", "codex", "kimi", "synthetic", "vertexai_claude"})


def dashboard_api_key(model_name: str, runtime_paths: RuntimePaths) -> str | None:
    """Return the key the dashboard saved for one model config (``model:<name>``), if any.

    ``name@level`` uses the key saved for ``name``.
    """
    service = f"model:{split_model_reference(model_name)[0]}"
    model_creds = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(service)
    return model_creds.get("api_key") if model_creds else None


def model_with_dashboard_key(model_name: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> ModelConfig:
    """Return the model with the key the dashboard saved for it as its ``api_key``, which beats a configured one."""
    if dashboard_key := dashboard_api_key(model_name, runtime_paths):
        return model_config.model_copy(update={"api_key": dashboard_key})
    return model_config


def own_api_key(model_config: ModelConfig) -> str | None:
    """Return the key a model's config sets, if any.

    ``extra_kwargs.client_params.api_key`` wins over ``api_key`` and ``extra_kwargs.api_key``, because the provider
    clients take ``client_params`` over every other client option.
    """
    client_params = (model_config.extra_kwargs or {}).get("client_params") or {}
    return client_params.get("api_key") or model_config.configured_api_key()


def api_key_env_reference(model_config: ModelConfig) -> str | None:
    """Return ``NAME`` when the key the model's route sends is written as the whole value ``${NAME}``, else None."""
    key = own_api_key(model_config)
    if provider_route(model_config.provider) in _KEYLESS_ROUTES or not isinstance(key, str):
        return None
    reference = ENV_PLACEHOLDER_PATTERN.fullmatch(key)
    return reference.group(1) if reference else None


def _needs_no_key(model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether the model authenticates another way and sets no ``client_params`` key, so it needs no key."""
    client_params = (model_config.extra_kwargs or {}).get("client_params") or {}
    return not client_params.get("api_key") and uses_alternative_auth(model_config, runtime_paths)


def unset_api_key_variable(model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    """Return the variable the model's key references when the model needs it and the runtime environment sets none."""
    name = api_key_env_reference(model_config)
    if not name or get_secret_from_env(name, runtime_paths) is not None or _needs_no_key(model_config, runtime_paths):
        return None
    return name


def resolve_api_key_reference(model_config: ModelConfig, runtime_paths: RuntimePaths) -> ModelConfig:
    """Return the model with the ``${NAME}`` key its route sends replaced by that runtime env variable's value.

    When the variable is unset, a model that authenticates another way loses the key, and any other model comes back
    unchanged; ``unset_api_key_variable`` names its variable.
    """
    name = api_key_env_reference(model_config)
    if not name:
        return model_config
    value = get_secret_from_env(name, runtime_paths)
    if value is None and not _needs_no_key(model_config, runtime_paths):
        return model_config
    extra_kwargs = model_config.extra_kwargs or {}
    client_params = extra_kwargs.get("client_params") or {}
    if client_params.get("api_key"):
        client_params = {**client_params, "api_key": value}
        return model_config.model_copy(update={"extra_kwargs": {**extra_kwargs, "client_params": client_params}})
    update: dict[str, object] = {"api_key": value}
    if "api_key" in extra_kwargs:
        update["extra_kwargs"] = {key: setting for key, setting in extra_kwargs.items() if key != "api_key"}
    return model_config.model_copy(update=update)


def gemini_uses_vertexai(model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a Gemini model runs on Vertex AI.

    The first of these the model sets, to true or false, decides: ``client_params.enterprise``, then
    ``client_params.vertexai``, as google-genai ranks them, since ``client_params`` wins over every client option, then
    ``extra_kwargs.vertexai``. Only a model that sets none of them follows ``GOOGLE_GENAI_USE_VERTEXAI``.
    """
    extra_kwargs = model_config.extra_kwargs or {}
    client_params = extra_kwargs.get("client_params") or {}
    for flag in (client_params.get("enterprise"), client_params.get("vertexai"), extra_kwargs.get("vertexai")):
        if flag is not None:
            return bool(flag)
    return runtime_paths.env_flag("GOOGLE_GENAI_USE_VERTEXAI")


def uses_alternative_auth(model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a model authenticates with a provider credential other than ``api_key``.

    Gemini on Vertex AI authenticates with Google Cloud credentials. Such a model never falls back to the provider's
    shared key; the provider adapters build it with this rule.
    """
    provider = provider_route(model_config.provider)
    if provider == "google":
        return gemini_uses_vertexai(model_config, runtime_paths)
    extra_kwargs = model_config.extra_kwargs or {}
    return any(extra_kwargs.get(name) for name in _ALTERNATIVE_AUTH_KWARGS.get(provider, ()))


def model_api_key(model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    """Return the key a model authenticates with: its configured key, else its provider's shared key.

    A model that authenticates another way (``uses_alternative_auth``) never receives the shared key.
    """
    configured = own_api_key(model_config)
    if configured is not None or uses_alternative_auth(model_config, runtime_paths):
        return configured
    return get_api_key_for_provider(model_config.provider, runtime_paths=runtime_paths)


def model_uses_own_credential(model_name: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a model authenticates without the provider's shared key.

    Its own credential is a key in config (``own_api_key``), a provider's alternative credential
    such as Anthropic's ``auth_token``, or the key saved for the model in the dashboard
    (``model:<name>``). Reading the dashboard key raises ``OSError`` or ``ValueError`` when the
    credential store cannot be opened.
    """
    return bool(
        own_api_key(model_config)
        or uses_alternative_auth(model_config, runtime_paths)
        or dashboard_api_key(model_name, runtime_paths),
    )


def missing_model_api_key_provider(config: Config, runtime_paths: RuntimePaths, model_name: str) -> str | None:
    """Return the provider service a configured model needs a key for when none resolves, else None.

    A model needs no shared key when ``model_uses_own_credential`` holds; otherwise the shared
    provider key (including its env-var-named twin) must resolve. Only providers that
    authenticate with one API key are checked, and unknown model names are left to config
    validation.
    """
    model_config = config.models.get(model_name)
    if model_config is None:
        return None
    provider = provider_route(model_config.provider)
    if provider == "ollama" or provider not in PROVIDER_ENV_KEYS:
        return None
    if model_uses_own_credential(model_name, model_config, runtime_paths):
        return None
    if get_api_key_for_provider(provider, runtime_paths=runtime_paths):
        return None
    return provider
