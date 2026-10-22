"""Which key a configured model authenticates with: provider spelling, the dashboard key, and missing-key checks.

The provider registry builds models with these rules; the CLI and the dashboard use them to report a model
that has no key before any request fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.credentials.sync import get_api_key_for_provider

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

__all__ = ["canonical_provider", "dashboard_api_key", "missing_model_api_key_provider", "model_uses_own_credential"]

# Settings the provider adapters accept in place of ``api_key``; Gemini with ``vertexai: true``
# authenticates with Google Cloud credentials instead.
_ALTERNATIVE_AUTH_KWARGS: dict[str, tuple[str, ...]] = {
    "anthropic": ("auth_token",),
    "azure": ("azure_ad_token", "azure_ad_token_provider"),
    "google": ("vertexai",),
}


def canonical_provider(provider: str) -> str:
    """Return the normalized provider key used for model dispatch and identity checks."""
    return provider.strip().lower().replace("-", "_")


def dashboard_api_key(model_name: str, runtime_paths: RuntimePaths) -> str | None:
    """Return the key the dashboard saved for one model config (``model:<name>``), if any."""
    model_creds = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(f"model:{model_name}")
    return model_creds.get("api_key") if model_creds else None


def _uses_alternative_auth(model_config: ModelConfig) -> bool:
    """Return whether a model authenticates with a provider credential other than ``api_key``."""
    provider = canonical_provider(model_config.provider)
    extra_kwargs = model_config.extra_kwargs or {}
    alternative_kwargs = _ALTERNATIVE_AUTH_KWARGS.get("google" if provider == "gemini" else provider, ())
    return any(extra_kwargs.get(name) for name in alternative_kwargs)


def model_uses_own_credential(model_name: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    """Return whether a model authenticates without the provider's shared key.

    Its own credential is a key in config (``api_key`` or ``extra_kwargs.api_key``), a
    provider's alternative credential such as Anthropic's ``auth_token``, or the key saved
    for the model in the dashboard (``model:<name>``). Reading the dashboard key raises
    ``OSError`` or ``ValueError`` when the credential store cannot be opened.
    """
    return bool(
        model_config.configured_api_key()
        or _uses_alternative_auth(model_config)
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
    provider = canonical_provider(model_config.provider)
    if provider == "gemini":
        provider = "google"
    if provider == "ollama" or provider not in PROVIDER_ENV_KEYS:
        return None
    if model_uses_own_credential(model_name, model_config, runtime_paths):
        return None
    if get_api_key_for_provider(provider, runtime_paths=runtime_paths):
        return None
    return provider
