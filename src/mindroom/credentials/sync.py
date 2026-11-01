"""Shared provider keys, as the runtime reads them from the shared credentials store.

The environment's provider variables reach the store as a read-time layer (see ``credentials/environment.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import EMBEDDER_CREDENTIAL_SERVICE, get_secret_from_env
from mindroom.credentials.manager import get_runtime_shared_credentials_manager
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT, OPENAI_BASE_URL_DEFAULT
from mindroom.providers.names import provider_route

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.constants import RuntimePaths

# Provider API-key services that also accept a dashboard credential saved under
# the env var name (for example ``OPENROUTER_API_KEY`` for ``openrouter``).
_PROVIDER_SERVICE_ENV_ALIASES = {k: v for k, v in PROVIDER_ENV_KEYS.items() if k != "ollama"}
_ENV_NAMED_PROVIDER_SERVICES = {v: k for k, v in _PROVIDER_SERVICE_ENV_ALIASES.items()}

# Sent when no embedder key resolves: the OpenAI SDK rejects client
# construction without a key, keyless local endpoints ignore the header, and
# keyed endpoints answer HTTP 401 through the classified failure path.
_EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY = "mindroom-keyless-placeholder"


def get_api_key_for_provider(provider: str, runtime_paths: RuntimePaths) -> str | None:
    """Get API key for a provider from the shared credentials store.

    A key saved in the dashboard wins over the provider's environment variable, which the store reads under it.

    Args:
        provider: The provider name (e.g., 'openai', 'anthropic')
        runtime_paths: Explicit runtime context for credential lookup.

    Returns:
        The API key if found, None otherwise

    """
    provider = provider_route(provider)
    # Special case for Ollama - return None as it doesn't use API keys
    if provider == "ollama":
        return None
    return get_api_key_for_service(provider, runtime_paths)


def provider_key_with_source(provider: str, runtime_paths: RuntimePaths) -> tuple[str | None, str | None]:
    """Return a provider's shared API key as ``get_api_key_for_provider`` resolves it, and the variable supplying it.

    The variable is None for a key saved in the dashboard, which wins over the environment.
    """
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    service, api_key = resolve_provider_service_api_key(provider_route(provider), creds_manager.get_api_key)
    return api_key, creds_manager.declared_variable(service) if api_key else None


def canonical_provider_service(service: str) -> str:
    """Return the canonical provider service for an env-var-named provider service.

    For example ``ANTHROPIC_API_KEY`` maps to ``anthropic``; any other service
    name is returned unchanged.
    """
    return _ENV_NAMED_PROVIDER_SERVICES.get(service, service)


def resolve_provider_service_api_key(
    service: str,
    load_api_key: Callable[[str], str | None],
) -> tuple[str, str | None]:
    """Resolve a provider key from its canonical service, else its env-var-named service.

    Users following env-var docs often save provider keys in the dashboard under
    the env var name (for example ``OPENROUTER_API_KEY``) instead of the canonical
    service (``openrouter``). The canonical service always wins; the env-var-named
    service is only a fallback for provider API-key services.

    Returns the service the key was read from together with the key, so callers
    such as the dashboard can report where the runtime actually finds it.
    """
    api_key = load_api_key(service)
    env_named_service = _PROVIDER_SERVICE_ENV_ALIASES.get(service)
    if env_named_service is None or (api_key and api_key.strip()):
        return service, api_key
    env_named_api_key = load_api_key(env_named_service)
    if env_named_api_key:
        return env_named_service, env_named_api_key
    return service, api_key


def get_api_key_for_service(service: str, runtime_paths: RuntimePaths) -> str | None:
    """Get an API key from one explicitly named shared credential service or its env-var-named twin."""
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    return resolve_provider_service_api_key(service, creds_manager.get_api_key)[1]


def get_embedder_api_key(
    runtime_paths: RuntimePaths,
    *,
    explicit_api_key: str | None = None,
    credentials_service: str | None = None,
) -> str:
    """Resolve the API key for the OpenAI-compatible semantic-search embedder.

    Resolution order:
    1. The explicit ``memory.embedder.config.api_key`` value from config.
    2. An explicitly configured credential service, when present. This is a
       strict binding: a missing key does not fall through to another service,
       except that a provider service (for example ``openrouter``) also accepts
       its env-var-named alias (``OPENROUTER_API_KEY``).
    3. Otherwise, the dedicated ``embedder`` service (declared by
       ``EMBEDDER_API_KEY`` / ``EMBEDDER_API_KEY_FILE``).
    4. Otherwise, the shared ``openai`` provider key (backward-compat fallback).
    5. ``_EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY``: the OpenAI SDK refuses to
       construct a client without a key, so keyless local endpoints get a
       non-secret placeholder they ignore, while a keyed endpoint rejects it
       with a loud classified auth failure instead of a construction crash.

    Blank values are treated as absent so they never shadow a real key, and
    resolved keys are stripped so stray whitespace never reaches the
    ``Authorization`` header.
    """
    if explicit_api_key and explicit_api_key.strip():
        return explicit_api_key.strip()
    if credentials_service is not None:
        service_api_key = get_api_key_for_service(credentials_service, runtime_paths)
        if service_api_key and service_api_key.strip():
            return service_api_key.strip()
        return _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY
    embedder_api_key = get_runtime_shared_credentials_manager(runtime_paths).get_api_key(EMBEDDER_CREDENTIAL_SERVICE)
    if embedder_api_key and embedder_api_key.strip():
        return embedder_api_key.strip()
    openai_api_key = get_api_key_for_provider("openai", runtime_paths=runtime_paths)
    if openai_api_key and openai_api_key.strip():
        return openai_api_key.strip()
    return _EMBEDDER_KEYLESS_PLACEHOLDER_API_KEY


def get_ollama_host(runtime_paths: RuntimePaths, *, configured_host: str | None, saved_first: bool) -> str:
    """Return the Ollama server a client reaches: ``configured_host`` or the saved ``OLLAMA_HOST``, else the default.

    ``configured_host`` wins unless ``saved_first``. The memory LLM and the Ollama embedder keep the saved-first order
    they had, so existing configs keep reaching the same server. The known case is the ``OLLAMA_HOST_DEFAULT`` host
    the dashboard's Memory page saved for every Ollama embedder, also where only ``OLLAMA_HOST`` names the real
    server, such as a Docker install's host.
    """
    if configured_host and not saved_first:
        return configured_host
    saved_host = (get_runtime_shared_credentials_manager(runtime_paths).load_credentials("ollama") or {}).get("host")
    return saved_host or configured_host or OLLAMA_HOST_DEFAULT


def get_openai_base_url(runtime_paths: RuntimePaths) -> str:
    """Return the endpoint an OpenAI client without one of its own reaches: ``OPENAI_BASE_URL``, else OpenAI's API.

    Passing it explicitly keeps the SDK from reading the variable itself, which it does only from the process env.
    """
    return get_secret_from_env("OPENAI_BASE_URL", runtime_paths=runtime_paths) or OPENAI_BASE_URL_DEFAULT


def get_ollama_api_key(runtime_paths: RuntimePaths) -> str | None:
    """Return ``OLLAMA_API_KEY``, the key an Ollama server that requires one gets from a caller with no key of its own."""
    return get_secret_from_env("OLLAMA_API_KEY", runtime_paths=runtime_paths)
