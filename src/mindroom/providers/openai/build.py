"""Build the OpenAI-family adapter for one configured model.

A model's key is its configured key (the provider registry folds in a key the dashboard saved for the model), else
the provider's shared key, which Azure and Z.ai may also take straight from their key variables. A route without a
usable key, a starter placeholder included, fails at its first request with the missing-key error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.environment import get_secret_from_env
from mindroom.llm.errors import MISSING_API_KEY, AuthenticationFailed, ProviderError
from mindroom.llm.provider import Capabilities, MediaKind, StructuredOutput
from mindroom.logging_config import get_logger
from mindroom.model_defaults import (
    AZURE_OPENAI_API_VERSION_DEFAULT,
    CEREBRAS_HOST_DEFAULT,
    DEEPSEEK_BASE_URL_DEFAULT,
    GROQ_HOST_DEFAULT,
    OPENAI_BASE_URL_DEFAULT,
    OPENROUTER_BASE_URL_DEFAULT,
    ZAI_BASE_URL_DEFAULT,
    claude_rejects_sampling_controls,
    openai_rejects_sampling_controls,
)
from mindroom.providers.credentials import model_api_key, own_api_key, uses_alternative_auth
from mindroom.providers.errors import CliLoginError, missing_api_key_error, provider_failure
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_keys
from mindroom.providers.identity import model_identity
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.names import provider_route
from mindroom.providers.openai.adapter_common import SDKClients
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.chat_wire import ChatWire, deepseek_request_thinks
from mindroom.providers.openai.codex_auth import CODEX_BASE_URL, CodexAuthError, borrow_codex_key
from mindroom.providers.openai.kimi_auth import KIMI_BASE_URL, KimiAuthError, borrow_kimi_token
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import ResponsesWire
from mindroom.providers.openai.settings import (
    CEREBRAS_FIELDS,
    CHAT_FIELDS,
    CODEX_FIELDS,
    GROQ_FIELDS,
    RESPONSES_FIELDS,
    ChatSettings,
    ResponsesSettings,
)
from mindroom.providers.openai.tool_search import openai_native_tool_search_supported
from mindroom.providers.prompt_cache_key import derive_agent_prompt_cache_key, derive_session_routing_key
from mindroom.runtime_env_policy import AZURE_OPENAI_ENV_BY_KEY

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx2
    from openai import AsyncOpenAI

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = [
    "ENDPOINT_VARIABLES",
    "OPENAI_FAMILY_PROVIDERS",
    "ProviderRuntime",
    "accepted_extra_kwargs",
    "build_provider",
]

logger = get_logger(__name__)

# Routes whose public endpoint and model ids support Responses native compaction.
_NATIVE_COMPACTION_ENDPOINTS = frozenset({OPENAI_BASE_URL_DEFAULT, CODEX_BASE_URL})
_NATIVE_COMPACTION_MODEL_PREFIXES = ("gpt-5.3-codex", "gpt-5.4", "gpt-6")
_NATIVE_COMPACTION_AUTHORED_KEYS = ("context_management", "previous_response_id", "background", "store")
# Explicit prompt-cache breakpoints exist on the public API from GPT-5.6.
_BREAKPOINT_MODEL = re.compile(r"gpt-(\d+)(?:\.(\d+))?(?:-|$)")


@dataclass(frozen=True, slots=True, kw_only=True)
class _ChatRoute:
    """The fixed facts of one Chat Completions route.

    The ``openai`` route's endpoint and key variable also serve Responses.
    """

    api_key_env: str | None
    base_url: str | None = None
    accepted: frozenset[str] = CHAT_FIELDS
    structured_output: StructuredOutput = "native"
    omitted_media: frozenset[MediaKind] = frozenset({"video"})
    # The runtime env variable that sets the endpoint when the config sets none.
    base_url_variable: str | None = None


_CHAT_ROUTES: dict[str, _ChatRoute] = {
    "openai": _ChatRoute(
        base_url=OPENAI_BASE_URL_DEFAULT,
        api_key_env=PROVIDER_ENV_KEYS["openai"],
        base_url_variable="OPENAI_BASE_URL",
    ),
    "azure": _ChatRoute(
        api_key_env=PROVIDER_ENV_KEYS["azure"],
        accepted=CHAT_FIELDS
        | {"api_version", "azure_endpoint", "azure_deployment", "azure_ad_token", "azure_ad_token_provider"},
    ),
    "openrouter": _ChatRoute(
        base_url=OPENROUTER_BASE_URL_DEFAULT,
        api_key_env=PROVIDER_ENV_KEYS["openrouter"],
        accepted=CHAT_FIELDS | {"models", "cache_system_prompt", "extended_cache_time"},
    ),
    "deepseek": _ChatRoute(
        base_url=DEEPSEEK_BASE_URL_DEFAULT,
        api_key_env=PROVIDER_ENV_KEYS["deepseek"],
        accepted=CHAT_FIELDS | {"use_thinking"},
        structured_output="forced_tool",
    ),
    "zai": _ChatRoute(api_key_env=None, base_url=ZAI_BASE_URL_DEFAULT),
    "llama_cpp": _ChatRoute(
        api_key_env=None,
        base_url="http://127.0.0.1:8080/v1",
    ),
    "kimi": _ChatRoute(
        api_key_env=None,
        base_url=KIMI_BASE_URL,
        accepted=CHAT_FIELDS | {"kimi_home", "prompt_cache_key"},
    ),
    "groq": _ChatRoute(
        api_key_env=PROVIDER_ENV_KEYS["groq"],
        accepted=GROQ_FIELDS,
        structured_output="forced_tool",
        omitted_media=frozenset({"audio", "file", "video"}),
        base_url_variable="GROQ_BASE_URL",
    ),
    "cerebras": _ChatRoute(
        api_key_env=PROVIDER_ENV_KEYS["cerebras"],
        accepted=CEREBRAS_FIELDS,
        structured_output="json_schema",
        omitted_media=frozenset({"audio", "image", "file", "video"}),
        base_url_variable="CEREBRAS_BASE_URL",
    ),
}
# The endpoint variable of every route that reads one, by route.
ENDPOINT_VARIABLES = {key: route.base_url_variable for key, route in _CHAT_ROUTES.items() if route.base_url_variable}
# Every route this module builds, as ``provider_route`` names it.
OPENAI_FAMILY_PROVIDERS = frozenset(_CHAT_ROUTES) | {"codex"}
# The ``extra_kwargs`` keys each Responses route accepts.
_RESPONSES_ACCEPTED: dict[str, frozenset[str]] = {"openai": RESPONSES_FIELDS, "codex": CODEX_FIELDS}
_ROUTE_KEY_VARIABLES = frozenset({"azure", "zai"})
# Providers whose models authenticate without the shared key.
_NO_SHARED_KEY = frozenset({"llama_cpp", "kimi"})


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderRuntime:
    """What building a provider needs beyond its config.

    ``execution_identity`` scopes the derived prompt-cache keys; ``codex_default_instructions`` is the configured
    prompt a Codex request sends as its instructions unless the model sets its own; ``http_client`` is the pooled
    client SDK clients send through.
    """

    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    codex_default_instructions: str
    http_client: httpx2.AsyncClient = MODEL_HTTP_CLIENT


def _configured_key(route_key: str, model_config: ModelConfig, runtime: ProviderRuntime) -> str | None:
    """Return the configured key, else the provider's shared key; routes in ``_NO_SHARED_KEY`` take no shared key."""
    if route_key in _NO_SHARED_KEY:
        return own_api_key(model_config)
    key = model_api_key(model_config, runtime.runtime_paths)
    # Azure and Z.ai keys may also come straight from their variables, including the ``_FILE`` forms.
    if key or uses_alternative_auth(model_config, runtime.runtime_paths) or route_key not in _ROUTE_KEY_VARIABLES:
        return key
    return get_secret_from_env(PROVIDER_ENV_KEYS[route_key], runtime_paths=runtime.runtime_paths)


def _provider_default_sampling(model_id: str) -> bool:
    """Return whether the model rejects a temperature, so overrides must leave sampling to the provider."""
    return claude_rejects_sampling_controls(model_id) or openai_rejects_sampling_controls(model_id)


def _authored_base_url(client_params: Mapping[str, Any] | None, base_url: str | None) -> str | None:
    """Return the endpoint a config sets; ``client_params`` wins over ``base_url``, as over every client option."""
    return (client_params or {}).get("base_url") or base_url


def _client_options(settings: ChatSettings | ResponsesSettings, *, base_url: str) -> dict[str, Any]:
    """Return the SDK client options a route configures; ``client_params`` is merged over them per client."""
    return {
        "organization": settings.organization,
        "base_url": base_url,
        "timeout": settings.timeout,
        "default_headers": settings.default_headers,
        "default_query": settings.default_query,
    }


def _sdk_client_kwargs(
    configured: Mapping[str, Any],
    settings: ChatSettings | ResponsesSettings,
    http_client: httpx2.AsyncClient,
) -> dict[str, Any]:
    """Return SDK client keywords: the configured values that are set, with ``client_params`` merged over them.

    ``client_params.base_url`` is already in the configured endpoint (Groq and Cerebras append their API path
    to it). The shared HTTP client and no SDK retries always win, since the request pipeline alone retries a failed
    request.
    """
    authored = {key: value for key, value in (settings.client_params or {}).items() if key != "base_url"}
    configured_values = {key: value for key, value in configured.items() if value is not None}
    return configured_values | authored | {"http_client": http_client, "max_retries": 0}


def _cli_login_error(error: CliLoginError, *, provider: str) -> ProviderError:
    """Return the error of a CLI login whose token could not be borrowed.

    A token refresh that failed on the network or with a transient status is the provider's transient failure, as
    ``provider_failure`` decides; any other login failure, such as a missing login or a refresh token the CLI's
    server rejected, is final and tells the user to log in again. It carries the status that server answered, or
    401 for a login the server never answered, as a missing API key does.
    """
    return provider_failure(error, provider=provider) or AuthenticationFailed(
        str(error),
        provider=provider,
        status_code=error.status_code or 401,
    )


def _openai_client(
    api_key: str,
    options: Mapping[str, Any],
    settings: ChatSettings | ResponsesSettings,
    http_client: httpx2.AsyncClient,
) -> AsyncOpenAI:
    from openai import AsyncOpenAI  # noqa: PLC0415 - keep the SDK out of module import time

    return AsyncOpenAI(**_sdk_client_kwargs({"api_key": api_key, **options}, settings, http_client))


def _route_base_url(
    route_key: str,
    client_params: Mapping[str, Any] | None,
    base_url: str | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Return a route's endpoint: the config's, else its endpoint variable's, else its default."""
    route = _CHAT_ROUTES[route_key]
    configured = _authored_base_url(client_params, base_url) or (
        get_secret_from_env(route.base_url_variable, runtime_paths=runtime_paths) if route.base_url_variable else None
    )
    # Groq and Cerebras endpoints are hosts, as configs for their SDKs give them; the route appends its API path.
    if route_key == "groq":
        return f"{(configured or GROQ_HOST_DEFAULT).rstrip('/')}/openai/v1"
    if route_key == "cerebras":
        return f"{(configured or CEREBRAS_HOST_DEFAULT).rstrip('/')}/v1"
    return configured or route.base_url or ""


def _keyed_client_factory(
    route_key: str,
    settings: ChatSettings | ResponsesSettings,
    *,
    api_key: str | None,
    options: Mapping[str, Any],
    provider: str,
    runtime: ProviderRuntime,
) -> Callable[[], AsyncOpenAI]:
    """Return the factory of a client keyed by ``api_key``, else failing with the missing-key error.

    A route with no key variable gets the ``not-provided`` placeholder key instead of failing.
    """
    api_key_env = _CHAT_ROUTES[route_key].api_key_env

    def client() -> AsyncOpenAI:
        if not api_key and api_key_env is not None:
            raise missing_api_key_error(api_key_env, provider=provider)
        # The SDK client requires a key, so keyless OpenAI-compatible endpoints get a placeholder.
        return _openai_client(api_key or "not-provided", options, settings, runtime.http_client)

    return client


def _chat_client_factory(
    route_key: str,
    settings: ChatSettings,
    *,
    provider: str,
    api_key: str | None,
    alternative_auth: bool,
    base_url: str,
    runtime: ProviderRuntime,
) -> Callable[[], AsyncOpenAI]:
    """Return the SDK client factory of one Chat route, whose errors name ``provider``.

    ``alternative_auth`` says the route authenticates without a key.
    """
    route = _CHAT_ROUTES[route_key]
    http_client = runtime.http_client
    options = _client_options(settings, base_url=base_url)
    if route_key == "kimi":

        def kimi_client() -> AsyncOpenAI:
            try:
                token = borrow_kimi_token(kimi_home=settings.kimi_home)
            except KimiAuthError as error:
                raise _cli_login_error(error, provider=provider) from error
            return _openai_client(token, options, settings, http_client)

        return kimi_client
    if route_key == "azure":
        api_key_env = route.api_key_env
        assert api_key_env is not None

        def azure_client() -> AsyncOpenAI:
            from openai import AsyncAzureOpenAI  # noqa: PLC0415 - keep the SDK out of module import time

            if not (api_key or alternative_auth):
                msg = (
                    "Azure OpenAI authentication not configured. Please provide one of: "
                    f"{api_key_env} environment variable, azure_ad_token, or azure_ad_token_provider"
                )
                raise AuthenticationFailed(msg, provider=provider, status_code=401, code=MISSING_API_KEY)
            azure: dict[str, Any] = {
                **options,
                # Azure has no default endpoint, and the SDK rejects an empty base URL beside azure_endpoint.
                "base_url": base_url or None,
                "api_key": api_key,
                "api_version": settings.api_version or AZURE_OPENAI_API_VERSION_DEFAULT,
                "azure_endpoint": settings.azure_endpoint,
                "azure_deployment": settings.azure_deployment,
                "azure_ad_token": settings.azure_ad_token,
                "azure_ad_token_provider": settings.azure_ad_token_provider,
            }
            return AsyncAzureOpenAI(**_sdk_client_kwargs(azure, settings, http_client))

        return azure_client
    return _keyed_client_factory(
        route_key,
        settings,
        api_key=api_key,
        options=options,
        provider=provider,
        runtime=runtime,
    )


def _with_env_setting(extra_kwargs: dict[str, Any], setting: str, variable: str, runtime: ProviderRuntime) -> None:
    """Fill ``setting`` from ``variable`` in the runtime env, the ``.env`` beside the config included, unless set."""
    if setting not in extra_kwargs and (value := get_secret_from_env(variable, runtime_paths=runtime.runtime_paths)):
        extra_kwargs[setting] = value


def _with_agent_cache_key(extra_kwargs: dict[str, Any], runtime: ProviderRuntime) -> None:
    """Share cache accounting across one agent's threads unless the config sets its own key."""
    if "prompt_cache_key" in extra_kwargs or runtime.execution_identity is None:
        return
    extra_kwargs["prompt_cache_key"] = derive_agent_prompt_cache_key(
        runtime.execution_identity,
        storage_root=runtime.runtime_paths.storage_root,
    )


def _populate_azure_env(extra_kwargs: dict[str, Any], runtime: ProviderRuntime) -> None:
    for setting, key in (
        ("azure_endpoint", "endpoint"),
        ("api_version", "api_version"),
        ("azure_deployment", "deployment"),
    ):
        _with_env_setting(extra_kwargs, setting, AZURE_OPENAI_ENV_BY_KEY[key], runtime)


def _build_chat(route_key: str, model_config: ModelConfig, runtime: ProviderRuntime) -> ChatAdapter:
    route = _CHAT_ROUTES[route_key]
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if route_key == "kimi":
        _with_env_setting(extra_kwargs, "kimi_home", "KIMI_CODE_HOME", runtime)
        _with_agent_cache_key(extra_kwargs, runtime)
    if route_key == "azure":
        _populate_azure_env(extra_kwargs, runtime)
    settings = settings_from_extra_kwargs(
        ChatSettings,
        extra_kwargs,
        provider=model_config.provider,
        accepted=route.accepted,
    )
    api_key = _configured_key(route_key, model_config, runtime)
    if route_key in {"openrouter", "zai"} and not api_key:
        logger.warning("No API key found in environment or CredentialsManager", provider=route_key)
    base_url = _route_base_url(route_key, settings.client_params, settings.base_url, runtime.runtime_paths)
    endpoint = (
        (settings.client_params or {}).get("azure_endpoint") or settings.azure_endpoint or base_url
        if route_key == "azure"
        else base_url
    )
    identity = model_identity(model_config, endpoint=endpoint.rstrip("/") if endpoint else None)
    structured_output = route.structured_output
    if route_key == "deepseek" and deepseek_request_thinks(settings):
        # DeepSeek rejects a forced tool call in thinking mode (https://api-docs.deepseek.com/api/create-chat-completion).
        structured_output = "json_mode"
    capabilities = Capabilities(
        structured_output=structured_output,
        omitted_media=route.omitted_media,
        url_media=frozenset({"image"}),
        provider_default_sampling=_provider_default_sampling(identity.id),
    )
    return ChatAdapter(
        identity=identity,
        capabilities=capabilities,
        # OpenAI's own API takes the system prompt as ``developer``; OpenAI-compatible servers, such as vLLM and
        # llama.cpp behind the ``openai`` route, expect ``system``.
        wire=ChatWire(
            route=route_key,
            system_role="developer" if identity.endpoint == OPENAI_BASE_URL_DEFAULT else "system",
        ),
        settings=settings,
        clients=SDKClients(
            _chat_client_factory(
                route_key,
                settings,
                provider=identity.provider,
                api_key=api_key,
                alternative_auth=uses_alternative_auth(model_config, runtime.runtime_paths),
                base_url=base_url,
                runtime=runtime,
            ),
            per_request=route_key == "kimi",
        ),
    )


def _supports_explicit_breakpoint(model_id: str, endpoint: str) -> bool:
    version = _BREAKPOINT_MODEL.match(model_id)
    if version is None or (int(version[1]), int(version[2] or 0)) < (5, 6):
        return False
    return endpoint == OPENAI_BASE_URL_DEFAULT


def _native_compaction_supported(model_id: str, endpoint: str, store: bool | None, settings: ResponsesSettings) -> bool:
    """Native compaction needs explicit replay on a public route and no authored replay policy."""
    request_params = settings.request_params or {}
    return (
        store is not True
        and model_id.startswith(_NATIVE_COMPACTION_MODEL_PREFIXES)
        and endpoint in _NATIVE_COMPACTION_ENDPOINTS
        and not any(
            key in params
            for params in (request_params, settings.extra_body or {}, request_params.get("extra_body") or {})
            for key in _NATIVE_COMPACTION_AUTHORED_KEYS
        )
    )


def _build_responses(
    route_key: Literal["openai", "codex"],
    model_config: ModelConfig,
    runtime: ProviderRuntime,
) -> ResponsesAdapter:
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if route_key == "codex":
        _with_env_setting(extra_kwargs, "codex_home", "CODEX_HOME", runtime)
        _with_agent_cache_key(extra_kwargs, runtime)
        if runtime.execution_identity is not None:
            extra_kwargs["session_id"] = derive_session_routing_key(
                runtime.execution_identity,
                storage_root=runtime.runtime_paths.storage_root,
            )
    settings = settings_from_extra_kwargs(
        ResponsesSettings,
        extra_kwargs,
        provider=model_config.provider,
        accepted=_RESPONSES_ACCEPTED[route_key],
    )
    # One storage setting drives both request building and history replay.
    store = (settings.request_params or {}).get("store", settings.store)
    if route_key == "codex":
        store = False if store is None else store
        base_url = _authored_base_url(settings.client_params, settings.base_url) or CODEX_BASE_URL
    else:
        base_url = _route_base_url("openai", settings.client_params, settings.base_url, runtime.runtime_paths)
    endpoint = base_url.rstrip("/")
    identity = model_identity(model_config, endpoint=endpoint)
    model_id = identity.id
    capabilities = Capabilities(
        structured_output="native",
        omitted_media=frozenset({"audio", "video"}),
        url_media=frozenset({"file", "image"}),
        native_tool_search=openai_native_tool_search_supported(route_key, model_id, base_url=base_url),
        native_compaction=_native_compaction_supported(model_id, endpoint, store, settings),
        provider_default_sampling=_provider_default_sampling(model_id),
    )
    wire = ResponsesWire(
        route=route_key,
        store=store,
        explicit_breakpoint=route_key == "openai" and _supports_explicit_breakpoint(model_id, endpoint),
        default_instructions=settings.default_instructions or runtime.codex_default_instructions,
    )
    http_client = runtime.http_client
    options = _client_options(settings, base_url=base_url)
    if route_key == "codex":

        def codex_client() -> AsyncOpenAI:
            try:
                token, account_id = borrow_codex_key(codex_home=settings.codex_home)
            except CodexAuthError as error:
                raise _cli_login_error(error, provider=identity.provider) from error
            headers = dict(settings.default_headers or {})
            if account_id:
                headers["ChatGPT-Account-ID"] = account_id
            return _openai_client(token, {**options, "default_headers": headers or None}, settings, http_client)

        clients = SDKClients(codex_client, per_request=True)
    else:
        clients = SDKClients(
            _keyed_client_factory(
                "openai",
                settings,
                api_key=_configured_key("openai", model_config, runtime),
                options=options,
                provider=identity.provider,
                runtime=runtime,
            ),
        )
    return ResponsesAdapter(identity=identity, capabilities=capabilities, wire=wire, settings=settings, clients=clients)


def _responses_route(model_config: ModelConfig, runtime_paths: RuntimePaths) -> Literal["openai", "codex"] | None:
    """Return the Responses route that serves one OpenAI-family model, or None when Chat Completions serves it.

    ``provider: openai`` uses Responses when ``api: responses`` is set, or when ``api`` is unset and the
    model and endpoint support native tool search.
    """
    route_key = provider_route(model_config.provider)
    if route_key == "codex":
        return "codex"
    if route_key != "openai":
        return None
    extra_kwargs = model_config.extra_kwargs or {}
    base_url = _route_base_url("openai", extra_kwargs.get("client_params"), extra_kwargs.get("base_url"), runtime_paths)
    if model_config.api == "responses" or (
        model_config.api is None and openai_native_tool_search_supported("openai", model_config.id, base_url=base_url)
    ):
        return "openai"
    return None


def accepted_extra_kwargs(model_config: ModelConfig, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return the ``extra_kwargs`` keys the route that serves one OpenAI-family model accepts."""
    if (responses_route := _responses_route(model_config, runtime_paths)) is not None:
        return settings_keys(ResponsesSettings, _RESPONSES_ACCEPTED[responses_route])
    return settings_keys(ChatSettings, _CHAT_ROUTES[provider_route(model_config.provider)].accepted)


def build_provider(model_config: ModelConfig, runtime: ProviderRuntime) -> ChatAdapter | ResponsesAdapter:
    """Return the adapter for one OpenAI-family model config, without the pipeline wrappers.

    The SDK loads here, with the model, so the model's first request does not import it.
    Raises ``ValueError`` for providers outside the family and for ``extra_kwargs`` the route does not accept.
    """
    # The registry reads this module's route tables when it is imported, so the SDK cannot load with the module.
    import openai  # noqa: F401, PLC0415 - keep the SDK out of module import time

    route_key = provider_route(model_config.provider)
    if route_key not in OPENAI_FAMILY_PROVIDERS:
        msg = f"Provider {model_config.provider!r} is not an OpenAI-family provider"
        raise ValueError(msg)
    if (responses_route := _responses_route(model_config, runtime.runtime_paths)) is not None:
        return _build_responses(responses_route, model_config, runtime)
    return _build_chat(route_key, model_config, runtime)
