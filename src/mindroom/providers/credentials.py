"""Which key a configured model authenticates with: the dashboard key and missing-key checks.

The provider registry builds models with these rules; the CLI and the dashboard use them to report a model
that has no key before any request fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import get_api_key_for_provider
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


def gemini_uses_vertexai(model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a Gemini model runs on Vertex AI.

    ``client_params`` wins over every client option, so a ``client_params.enterprise`` set to true or false decides
    first, else a ``client_params.vertexai``, as google-genai ranks them. Otherwise a true ``extra_kwargs.vertexai`` or a
    true ``GOOGLE_GENAI_USE_VERTEXAI`` selects Vertex AI; ``extra_kwargs.vertexai: false`` overrides nothing.
    """
    extra_kwargs = model_config.extra_kwargs or {}
    client_params = extra_kwargs.get("client_params") or {}
    for flag in ("enterprise", "vertexai"):
        if client_params.get(flag) is not None:
            return bool(client_params[flag])
    return bool(extra_kwargs.get("vertexai")) or runtime_paths.env_flag("GOOGLE_GENAI_USE_VERTEXAI")


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
    configured = model_config.configured_api_key()
    if configured is not None or uses_alternative_auth(model_config, runtime_paths):
        return configured
    return get_api_key_for_provider(model_config.provider, runtime_paths=runtime_paths)


def model_uses_own_credential(model_name: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a model authenticates without the provider's shared key.

    Its own credential is a key in config (``api_key`` or ``extra_kwargs.api_key``), a
    provider's alternative credential such as Anthropic's ``auth_token``, or the key saved
    for the model in the dashboard (``model:<name>``). Reading the dashboard key raises
    ``OSError`` or ``ValueError`` when the credential store cannot be opened.
    """
    return bool(
        model_config.configured_api_key()
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
