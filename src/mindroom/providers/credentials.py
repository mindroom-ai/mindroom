"""Which key a configured model authenticates with: the dashboard key and missing-key checks.

The provider registry builds models with these rules; the CLI and the dashboard use them to report a model
that has no key before any request fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.schema_hints import ENV_PLACEHOLDER_PATTERN
from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.llm.errors import AuthenticationFailed
from mindroom.providers.names import provider_route

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

__all__ = [
    "dashboard_api_key",
    "gemini_uses_vertexai",
    "missing_model_api_key_provider",
    "model_api_key",
    "model_uses_own_credential",
    "own_api_key",
    "resolve_api_key_references",
    "uses_alternative_auth",
]

# Settings the provider adapters accept in place of ``api_key``.
_ALTERNATIVE_AUTH_KWARGS: dict[str, tuple[str, ...]] = {
    "anthropic": ("auth_token",),
    "azure": ("azure_ad_token", "azure_ad_token_provider"),
}


def dashboard_api_key(model_name: str, runtime_paths: RuntimePaths) -> str | None:
    """Return the key the dashboard saved for one model config (``model:<name>``), if any."""
    model_creds = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(f"model:{model_name}")
    return model_creds.get("api_key") if model_creds else None


def own_api_key(model_config: ModelConfig) -> str | None:
    """Return the key a model's config sets, if any.

    ``extra_kwargs.client_params.api_key`` wins over ``api_key`` and ``extra_kwargs.api_key``, because the provider
    clients take ``client_params`` over every other client option.
    """
    client_params = (model_config.extra_kwargs or {}).get("client_params") or {}
    return client_params.get("api_key") or model_config.configured_api_key()


def resolve_api_key_references(model_config: ModelConfig, runtime_paths: RuntimePaths) -> ModelConfig:
    """Return the model with each key written as ``${NAME}`` replaced by that runtime env variable's value.

    Covers ``api_key``, ``extra_kwargs.api_key`` and ``extra_kwargs.client_params.api_key``; only a whole-value
    reference resolves. An unset or blank variable raises ``AuthenticationFailed`` naming it.
    """

    def resolve(key: object) -> object:
        if not (isinstance(key, str) and (reference := ENV_PLACEHOLDER_PATTERN.fullmatch(key))):
            return key
        name = reference.group(1)
        if (value := get_secret_from_env(name, runtime_paths)) is None:
            msg = f"{name} not set. This model's api_key reads the {name} environment variable."
            raise AuthenticationFailed(msg, provider=model_config.provider, status_code=401)
        return value

    extra_kwargs = dict(model_config.extra_kwargs or {})
    if "api_key" in extra_kwargs:
        extra_kwargs["api_key"] = resolve(extra_kwargs["api_key"])
    client_params = extra_kwargs.get("client_params") or {}
    if "api_key" in client_params:
        extra_kwargs["client_params"] = {**client_params, "api_key": resolve(client_params["api_key"])}
    update = {"api_key": resolve(model_config.api_key), "extra_kwargs": extra_kwargs or model_config.extra_kwargs}
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
