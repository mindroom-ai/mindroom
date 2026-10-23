"""Build the OpenAI-family adapter for one configured model.

A model's key is its configured key (the provider registry folds in a key the dashboard saved for the model),
then the provider's shared key, then the provider's own key variable. A route without a usable key fails at its
first request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mindroom.constants import PROVIDER_ENV_KEYS
from mindroom.credentials.sync import get_api_key_for_provider, get_secret_from_env
from mindroom.llm.errors import MISSING_API_KEY, AuthenticationFailed, ProviderError
from mindroom.llm.provider import Capabilities, MediaKind, ModelIdentity
from mindroom.logging_config import get_logger
from mindroom.model_defaults import (
    CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES,
    OPENAI_PROVIDER_DEFAULT_SAMPLING_MODEL_PREFIXES,
    OPENROUTER_BASE_URL_DEFAULT,
    ZAI_BASE_URL_DEFAULT,
)
from mindroom.prompts import CODEX_DEFAULT_INSTRUCTIONS
from mindroom.providers.errors import CliLoginError, provider_failure
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs
from mindroom.providers.model_http_client import MODEL_HTTP_CLIENT
from mindroom.providers.model_loading import uses_alternative_auth
from mindroom.providers.names import canonical_provider
from mindroom.providers.openai.adapter_common import SDKClients
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.chat_wire import ChatDialect, ChatWire
from mindroom.providers.openai.codex_auth import (
    CODEX_BASE_URL,
    CodexAuthError,
    borrow_codex_key,
    normalize_codex_model_id,
)
from mindroom.providers.openai.errors import missing_api_key_error
from mindroom.providers.openai.kimi_auth import KIMI_BASE_URL, KimiAuthError, borrow_kimi_token, normalize_kimi_model_id
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

    import httpx
    from openai import AsyncOpenAI

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

__all__ = ["ENDPOINT_VARIABLES", "OPENAI_FAMILY_PROVIDERS", "ProviderRuntime", "build_provider"]

logger = get_logger(__name__)

_OPENAI_API_BASE_URL = "https://api.openai.com/v1"
# The runtime env variable that sets the ``openai`` endpoint, for Chat and Responses alike, when the config sets none.
_OPENAI_BASE_URL_VARIABLE = "OPENAI_BASE_URL"
# Routes whose public endpoint and model ids support Responses native compaction.
_NATIVE_COMPACTION_ENDPOINTS = frozenset({_OPENAI_API_BASE_URL, CODEX_BASE_URL})
_NATIVE_COMPACTION_MODEL_PREFIXES = ("gpt-5.3-codex", "gpt-5.4", "gpt-6")
_NATIVE_COMPACTION_AUTHORED_KEYS = ("context_management", "previous_response_id", "background", "store")
# Explicit prompt-cache breakpoints exist on the public API from GPT-5.6.
_BREAKPOINT_MODEL = re.compile(r"gpt-(\d+)(?:\.(\d+))?(?:-|$)")
_CHAT_OMITTED_MEDIA: frozenset[MediaKind] = frozenset({"video"})
_ALL_MEDIA: frozenset[MediaKind] = frozenset({"audio", "image", "file", "video"})


@dataclass(frozen=True, slots=True, kw_only=True)
class _ChatRoute:
    """The fixed facts of one Chat Completions route."""

    provider: str
    provider_name: str
    base_url: str | None
    api_key_env: str | None
    dialect: ChatDialect
    accepted: frozenset[str]
    structured_output: Literal["native", "json_schema", "json_mode"] = "native"
    omitted_media: frozenset[MediaKind] = _CHAT_OMITTED_MEDIA
    developer_system_role: bool = False
    # The runtime env variable that sets the endpoint when the config sets none.
    base_url_variable: str | None = None


_CHAT_ROUTES: dict[str, _ChatRoute] = {
    "openai": _ChatRoute(
        provider="OpenAI",
        provider_name="OpenAI Chat",
        base_url=None,
        api_key_env="OPENAI_API_KEY",
        dialect="openai",
        accepted=CHAT_FIELDS,
        developer_system_role=True,
        base_url_variable=_OPENAI_BASE_URL_VARIABLE,
    ),
    "azure": _ChatRoute(
        provider="Azure",
        provider_name="Azure",
        base_url=None,
        api_key_env=AZURE_OPENAI_ENV_BY_KEY["api_key"],
        dialect="openai",
        accepted=CHAT_FIELDS
        | {"api_version", "azure_endpoint", "azure_deployment", "azure_ad_token", "azure_ad_token_provider"},
    ),
    "openrouter": _ChatRoute(
        provider="OpenRouter",
        provider_name="OpenRouter",
        base_url=OPENROUTER_BASE_URL_DEFAULT,
        api_key_env="OPENROUTER_API_KEY",
        dialect="openai",
        accepted=CHAT_FIELDS | {"models", "cache_system_prompt", "extended_cache_time"},
    ),
    "deepseek": _ChatRoute(
        provider="DeepSeek",
        provider_name="DeepSeek",
        base_url="https://api.deepseek.com",
        api_key_env="DEEPSEEK_API_KEY",
        dialect="openai",
        accepted=CHAT_FIELDS | {"use_thinking"},
        structured_output="json_mode",
    ),
    "zai": _ChatRoute(
        provider="ZAI",
        provider_name="ZAI",
        base_url=ZAI_BASE_URL_DEFAULT,
        api_key_env=None,
        dialect="openai",
        accepted=CHAT_FIELDS,
    ),
    "llama_cpp": _ChatRoute(
        provider="LlamaCpp",
        provider_name="LlamaCpp",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env=None,
        dialect="openai",
        accepted=CHAT_FIELDS,
    ),
    "kimi": _ChatRoute(
        provider="Kimi",
        provider_name="Kimi Chat",
        base_url=KIMI_BASE_URL,
        api_key_env=None,
        dialect="openai",
        accepted=CHAT_FIELDS | {"kimi_home", "prompt_cache_key"},
    ),
    "groq": _ChatRoute(
        provider="Groq",
        provider_name="Groq",
        base_url=None,
        api_key_env="GROQ_API_KEY",
        dialect="groq",
        accepted=GROQ_FIELDS,
        structured_output="json_mode",
        omitted_media=frozenset({"audio", "file", "video"}),
        base_url_variable="GROQ_BASE_URL",
    ),
    "cerebras": _ChatRoute(
        provider="Cerebras",
        provider_name="Cerebras",
        base_url=None,
        api_key_env="CEREBRAS_API_KEY",
        dialect="cerebras",
        accepted=CEREBRAS_FIELDS,
        structured_output="json_schema",
        omitted_media=_ALL_MEDIA,
        base_url_variable="CEREBRAS_BASE_URL",
    ),
}
# The endpoint variable of every route that reads one, by ``provider`` value.
ENDPOINT_VARIABLES = {key: route.base_url_variable for key, route in _CHAT_ROUTES.items() if route.base_url_variable}
_ROUTE_ALIASES = {"openai_codex": "codex", "kimi_code": "kimi"}
# Every ``provider`` value this module builds, after canonicalization.
OPENAI_FAMILY_PROVIDERS = frozenset(_CHAT_ROUTES) | {"codex", "openai_codex", "kimi_code"}
_ROUTE_KEY_VARIABLES = {"azure": AZURE_OPENAI_ENV_BY_KEY["api_key"], "zai": PROVIDER_ENV_KEYS["zai"]}
# Providers whose models authenticate without the shared key.
_NO_SHARED_KEY = frozenset({"llama_cpp", "kimi"})


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderRuntime:
    """What building a provider needs beyond its config.

    ``execution_identity`` scopes the derived prompt-cache keys; ``http_client`` is the pooled client SDK
    clients send through.
    """

    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None = None
    codex_default_instructions: str = CODEX_DEFAULT_INSTRUCTIONS
    http_client: httpx.AsyncClient = MODEL_HTTP_CLIENT


def _configured_key(route_key: str, model_config: ModelConfig, runtime: ProviderRuntime) -> str | None:
    """Return the configured key, else the provider's shared key."""
    key = model_config.configured_api_key()
    if key or uses_alternative_auth(model_config) or route_key in _NO_SHARED_KEY:
        return key or None
    shared = get_api_key_for_provider(route_key, runtime_paths=runtime.runtime_paths)
    # Azure and Z.ai keys may also come straight from their variables, including the ``_FILE`` forms.
    if shared or route_key not in _ROUTE_KEY_VARIABLES:
        return shared
    return get_secret_from_env(_ROUTE_KEY_VARIABLES[route_key], runtime_paths=runtime.runtime_paths)


def _provider_default_sampling(model_id: str) -> bool:
    """Return whether the model rejects a temperature, so overrides must leave sampling to the provider."""
    lowered = model_id.casefold()
    return lowered.endswith(CLAUDE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES) or lowered.rsplit("/", 1)[-1].startswith(
        OPENAI_PROVIDER_DEFAULT_SAMPLING_MODEL_PREFIXES,
    )


def _authored_base_url(client_params: Mapping[str, Any] | None, base_url: str | None) -> str | None:
    """Return the endpoint a config sets; ``client_params.base_url`` overrides ``base_url``, as in Agno."""
    return (client_params or {}).get("base_url") or base_url


def _client_options(
    settings: ChatSettings | ResponsesSettings,
    *,
    base_url: str,
    organization: bool = True,
) -> dict[str, Any]:
    """Return the SDK client options a route configures; ``client_params`` is merged over them per client."""
    return {
        "organization": settings.organization if organization else None,
        "base_url": base_url,
        "timeout": settings.timeout,
        "max_retries": settings.max_retries,
        "default_headers": settings.default_headers,
        "default_query": settings.default_query,
    }


def _sdk_client_kwargs(
    configured: Mapping[str, Any],
    settings: ChatSettings | ResponsesSettings,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Return SDK client keywords in Agno's precedence: the configured values that are set, then ``client_params``.

    ``client_params.base_url`` is already in the configured endpoint (Groq and Cerebras append their API path
    to it), and the shared HTTP client always wins.
    """
    authored = {key: value for key, value in (settings.client_params or {}).items() if key != "base_url"}
    return (
        {key: value for key, value in configured.items() if value is not None} | authored | {"http_client": http_client}
    )


def _cli_login_error(error: CliLoginError, *, provider: str) -> ProviderError:
    """Return the error of a CLI login whose token could not be borrowed.

    A token refresh that failed on the network or with a transient status is the provider's transient failure, as
    ``provider_failure`` decides; any other login failure, such as a missing login or a refresh token the CLI's
    server rejected, is final and tells the user to log in again.
    """
    return provider_failure(error, provider=provider) or AuthenticationFailed(
        str(error),
        provider=provider,
        status_code=401,
    )


def _openai_client(
    api_key: str,
    options: Mapping[str, Any],
    settings: ChatSettings | ResponsesSettings,
    http_client: httpx.AsyncClient,
) -> AsyncOpenAI:
    from openai import AsyncOpenAI  # noqa: PLC0415 - keep the SDK out of module import time

    return AsyncOpenAI(**_sdk_client_kwargs({"api_key": api_key, **options}, settings, http_client))


def _chat_base_url(route_key: str, route: _ChatRoute, settings: ChatSettings, runtime: ProviderRuntime) -> str:
    configured = _authored_base_url(settings.client_params, settings.base_url) or (
        runtime.runtime_paths.env_value(route.base_url_variable) if route.base_url_variable else None
    )
    if route_key == "openai":
        return configured or _OPENAI_API_BASE_URL
    if route_key == "groq":
        # The Groq SDK's base URL was a host; its paths start with /openai/v1.
        return f"{(configured or 'https://api.groq.com').rstrip('/')}/openai/v1"
    if route_key == "cerebras":
        return f"{(configured or 'https://api.cerebras.ai').rstrip('/')}/v1"
    return configured or route.base_url or ""


def _chat_client_factory(
    route_key: str,
    route: _ChatRoute,
    settings: ChatSettings,
    *,
    api_key: str | None,
    alternative_auth: bool,
    base_url: str,
    runtime: ProviderRuntime,
) -> Callable[[], AsyncOpenAI]:
    """Return the SDK client factory of one Chat route; ``alternative_auth`` says it authenticates without a key."""
    http_client = runtime.http_client
    process_env = runtime.runtime_paths.process_env
    options = _client_options(settings, base_url=base_url, organization=route.dialect == "openai")
    if route_key == "kimi":

        def kimi_client() -> AsyncOpenAI:
            try:
                token = borrow_kimi_token(kimi_home=settings.kimi_home)
            except KimiAuthError as error:
                raise _cli_login_error(error, provider=route.provider_name) from error
            return _openai_client(token, options, settings, http_client)

        return kimi_client
    if route_key == "azure":

        def azure_client() -> AsyncOpenAI:
            from openai import AsyncAzureOpenAI  # noqa: PLC0415 - keep the SDK out of module import time

            key = api_key or process_env.get(AZURE_OPENAI_ENV_BY_KEY["api_key"])
            if not (key or alternative_auth):
                msg = (
                    "Azure OpenAI authentication not configured. Please provide one of: "
                    "AZURE_OPENAI_API_KEY environment variable, azure_ad_token, or azure_ad_token_provider"
                )
                raise AuthenticationFailed(msg, provider=route.provider_name, status_code=401, code=MISSING_API_KEY)
            azure: dict[str, Any] = {
                "api_key": key,
                "api_version": settings.api_version or "2024-10-21",
                "organization": settings.organization,
                "azure_endpoint": settings.azure_endpoint or process_env.get(AZURE_OPENAI_ENV_BY_KEY["endpoint"]),
                "azure_deployment": settings.azure_deployment or process_env.get(AZURE_OPENAI_ENV_BY_KEY["deployment"]),
                "base_url": base_url or None,
                "azure_ad_token": settings.azure_ad_token,
                "azure_ad_token_provider": settings.azure_ad_token_provider,
                "default_headers": settings.default_headers,
                "default_query": settings.default_query,
            }
            return AsyncAzureOpenAI(**_sdk_client_kwargs(azure, settings, http_client))

        return azure_client

    def client() -> AsyncOpenAI:
        key = api_key or (process_env.get(route.api_key_env) if route.api_key_env else None)
        if not key:
            if route.api_key_env is not None:
                raise missing_api_key_error(route.api_key_env, provider=route.provider_name)
            # OpenAI-compatible local and keyless endpoints took Agno's placeholder key.
            key = "not-provided"
        return _openai_client(key, options, settings, http_client)

    return client


def _with_agent_cache_key(extra_kwargs: dict[str, Any], runtime: ProviderRuntime) -> None:
    """Share cache accounting across one agent's threads unless the config sets its own key."""
    if "prompt_cache_key" in extra_kwargs or runtime.execution_identity is None:
        return
    extra_kwargs["prompt_cache_key"] = derive_agent_prompt_cache_key(
        runtime.execution_identity,
        storage_root=runtime.runtime_paths.storage_root,
    )


def _populate_azure_env(extra_kwargs: dict[str, Any], runtime: ProviderRuntime) -> None:
    paths = runtime.runtime_paths
    if "azure_endpoint" not in extra_kwargs and (
        endpoint := get_secret_from_env(AZURE_OPENAI_ENV_BY_KEY["endpoint"], runtime_paths=paths)
    ):
        extra_kwargs["azure_endpoint"] = endpoint
    if "api_version" not in extra_kwargs and (version := paths.env_value(AZURE_OPENAI_ENV_BY_KEY["api_version"])):
        extra_kwargs["api_version"] = version
    if "azure_deployment" not in extra_kwargs and (
        deployment := paths.env_value(AZURE_OPENAI_ENV_BY_KEY["deployment"])
    ):
        extra_kwargs["azure_deployment"] = deployment


def _build_chat(route_key: str, model_id: str, model_config: ModelConfig, runtime: ProviderRuntime) -> ChatAdapter:
    route = _CHAT_ROUTES[route_key]
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if route_key == "kimi":
        model_id = normalize_kimi_model_id(model_id)
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
    base_url = _chat_base_url(route_key, route, settings, runtime)
    endpoint = (
        (settings.client_params or {}).get("azure_endpoint") or settings.azure_endpoint or base_url
        if route_key == "azure"
        else base_url
    )
    identity = ModelIdentity(
        id=model_id,
        provider=route.provider,
        provider_name=route.provider_name,
        endpoint=endpoint.rstrip("/") if endpoint else None,
    )
    capabilities = Capabilities(
        structured_output=route.structured_output,
        omitted_media=route.omitted_media,
        provider_default_sampling=_provider_default_sampling(model_id),
    )
    wire = ChatWire(
        route=route_key,
        dialect=route.dialect,
        developer_system_role=route.developer_system_role,
        replay_reasoning_content=route_key == "deepseek",
        replay_reasoning_details=route_key == "openrouter",
    )
    return ChatAdapter(
        identity=identity,
        capabilities=capabilities,
        wire=wire,
        settings=settings,
        clients=SDKClients(
            _chat_client_factory(
                route_key,
                route,
                settings,
                api_key=api_key,
                alternative_auth=uses_alternative_auth(model_config),
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
    return endpoint == _OPENAI_API_BASE_URL


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
    model_id: str,
    model_config: ModelConfig,
    runtime: ProviderRuntime,
) -> ResponsesAdapter:
    extra_kwargs = dict(model_config.extra_kwargs or {})
    accepted = RESPONSES_FIELDS
    if route_key == "codex":
        model_id = normalize_codex_model_id(model_id)
        accepted = CODEX_FIELDS
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
        accepted=accepted,
    )
    # One storage setting drives both request building and history replay.
    store = (settings.request_params or {}).get("store", settings.store)
    authored_base_url = _authored_base_url(settings.client_params, settings.base_url)
    if route_key == "codex":
        store = False if store is None else store
        base_url = authored_base_url or CODEX_BASE_URL
        provider, provider_name = "OpenAI Codex", "OpenAI Codex Responses"
    else:
        base_url = (
            authored_base_url or runtime.runtime_paths.env_value(_OPENAI_BASE_URL_VARIABLE) or _OPENAI_API_BASE_URL
        )
        provider, provider_name = "OpenAI", "OpenAI Responses"
    endpoint = base_url.rstrip("/")
    identity = ModelIdentity(id=model_id, provider=provider, provider_name=provider_name, endpoint=endpoint)
    capabilities = Capabilities(
        structured_output="native",
        omitted_media=frozenset({"audio", "video"}),
        native_tool_search=openai_native_tool_search_supported(
            route_key,
            model_id,
            base_url=base_url if route_key == "openai" else None,
        ),
        native_compaction=_native_compaction_supported(model_id, endpoint, store, settings),
        approval_receipt_after_response_id=True,
        # Codex rejects a temperature (``CODEX_REJECTED_FIELDS``), so it keeps its own sampling on every model.
        provider_default_sampling=route_key == "codex" or _provider_default_sampling(model_id),
        streams_only=route_key == "codex",
    )
    wire = ResponsesWire(
        route=route_key,
        provider_name=provider_name,
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
                raise _cli_login_error(error, provider=provider_name) from error
            headers = dict(settings.default_headers or {})
            if account_id:
                headers["ChatGPT-Account-ID"] = account_id
            return _openai_client(token, {**options, "default_headers": headers or None}, settings, http_client)

        return ResponsesAdapter(
            identity=identity,
            capabilities=capabilities,
            wire=wire,
            settings=settings,
            clients=SDKClients(codex_client, per_request=True),
        )
    api_key_env = "OPENAI_API_KEY"
    api_key = _configured_key("openai", model_config, runtime) or runtime.runtime_paths.process_env.get(api_key_env)

    def openai_client() -> AsyncOpenAI:
        if not api_key:
            raise missing_api_key_error(api_key_env, provider=provider_name)
        return _openai_client(api_key, options, settings, http_client)

    return ResponsesAdapter(
        identity=identity,
        capabilities=capabilities,
        wire=wire,
        settings=settings,
        clients=SDKClients(openai_client),
    )


def build_provider(model_config: ModelConfig, runtime: ProviderRuntime) -> ChatAdapter | ResponsesAdapter:
    """Return the adapter for one OpenAI-family model config, without the pipeline wrappers.

    ``provider: openai`` uses Responses when ``api: responses`` is set, or when ``api`` is unset and the
    model and endpoint support native tool search; otherwise Chat Completions.
    Raises ``ValueError`` for providers outside the family and for ``extra_kwargs`` the route does not accept.
    """
    provider = canonical_provider(model_config.provider)
    route_key = _ROUTE_ALIASES.get(provider, provider)
    model_id = model_config.id
    if route_key == "codex":
        return _build_responses("codex", model_id, model_config, runtime)
    if route_key == "openai":
        extra_kwargs = model_config.extra_kwargs or {}
        base_url = _authored_base_url(
            extra_kwargs.get("client_params"),
            extra_kwargs.get("base_url"),
        ) or runtime.runtime_paths.env_value(_OPENAI_BASE_URL_VARIABLE)
        if model_config.api == "responses" or (
            model_config.api is None and openai_native_tool_search_supported("openai", model_id, base_url=base_url)
        ):
            return _build_responses("openai", model_id, model_config, runtime)
    if route_key not in _CHAT_ROUTES:
        msg = f"Provider {model_config.provider!r} is not an OpenAI-family provider"
        raise ValueError(msg)
    return _build_chat(route_key, model_id, model_config, runtime)
