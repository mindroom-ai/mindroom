"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom import constants
from mindroom.constants import env_flag
from mindroom.credentials import get_credentials_manager, load_scoped_credentials
from mindroom.tool_system.worker_routing import (
    SHARED_ONLY_INTEGRATION_NAMES,
    WorkerScope,
    get_tool_execution_identity,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    shared_storage_root,
)
from mindroom.workers.models import WorkerHandle, WorkerSpec, worker_api_endpoint
from mindroom.workers.runtime import (
    get_primary_worker_manager,
    primary_worker_backend_available,
    primary_worker_backend_is_dedicated,
    primary_worker_backend_name,
)

if TYPE_CHECKING:
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

    from mindroom.workers.manager import WorkerManager

_SANDBOX_PROXY_EXECUTE_PATH = "/api/sandbox-runner/execute"
_SANDBOX_PROXY_LEASE_PATH = "/api/sandbox-runner/leases"
_SANDBOX_PROXY_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
_LOCAL_ONLY_SANDBOX_TOOLS = frozenset(SHARED_ONLY_INTEGRATION_NAMES - {"google", "spotify"})

_SANDBOX_RUNNER_MODE = env_flag("MINDROOM_SANDBOX_RUNNER_MODE")


def _read_proxy_url() -> str | None:
    value = os.getenv("MINDROOM_SANDBOX_PROXY_URL", "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _read_proxy_token() -> str | None:
    value = os.getenv("MINDROOM_SANDBOX_PROXY_TOKEN", "").strip()
    if not value:
        return None
    return value


def _read_proxy_timeout() -> float:
    raw = os.getenv("MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS", str(_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS))
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def _read_execution_mode() -> str | None:
    raw = os.getenv("MINDROOM_SANDBOX_EXECUTION_MODE")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    return normalized


def _read_credential_lease_ttl() -> int:
    raw = os.getenv("MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS", str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS))
    try:
        ttl_seconds = int(raw)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(execution_mode: str | None) -> set[str] | None:
    default = "" if execution_mode in {"selective", "sandbox_selective"} else "*"
    raw_value = os.getenv("MINDROOM_SANDBOX_PROXY_TOOLS", default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


# Parsed once at module load — these don't change at runtime.
_PROXY_URL = _read_proxy_url()
_PROXY_TOKEN = _read_proxy_token()
_PROXY_TIMEOUT = _read_proxy_timeout()
_EXECUTION_MODE = _read_execution_mode()
_CREDENTIAL_LEASE_TTL = _read_credential_lease_ttl()
_PROXY_TOOLS = _read_proxy_tools(_EXECUTION_MODE)


def sandbox_proxy_token_matches(provided_token: str | None) -> bool:
    """Validate a provided token against the configured shared token."""
    if _PROXY_TOKEN is None or provided_token is None:
        return False
    return hmac.compare_digest(provided_token, _PROXY_TOKEN)


def to_json_compatible(value: object) -> object:
    """Convert arbitrary values into JSON-friendly structures."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): to_json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_compatible(item) for item in value]
    return str(value)


def _read_credential_policy() -> dict[str, tuple[str, ...]]:
    raw_policy = os.getenv("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", "").strip()
    if not raw_policy:
        return {}

    try:
        parsed = json.loads(raw_policy)
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    policy: dict[str, tuple[str, ...]] = {}
    for selector, services in parsed.items():
        if not isinstance(selector, str):
            continue
        if not isinstance(services, list):
            continue
        cleaned_services = tuple(
            service.strip() for service in services if isinstance(service, str) and service.strip()
        )
        policy[selector.strip()] = cleaned_services
    return policy


_CREDENTIAL_POLICY = _read_credential_policy()


def _credential_services_for_call(tool_name: str, function_name: str) -> list[str]:
    policy = _CREDENTIAL_POLICY
    selectors = ("*", tool_name, f"{tool_name}.{function_name}")
    services: list[str] = []
    for selector in selectors:
        for service in policy.get(selector, ()):
            if service not in services:
                services.append(service)
    return services


def _filter_internal_credential_keys(credentials: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in credentials.items() if not str(key).startswith("_")}


def _collect_credential_overrides(
    tool_name: str,
    function_name: str,
    *,
    worker_scope: WorkerScope | None,
    routing_agent_name: str | None,
) -> dict[str, object]:
    services = _credential_services_for_call(tool_name, function_name)
    if not services:
        return {}

    credentials_manager = get_credentials_manager()
    merged_overrides: dict[str, object] = {}
    for service in services:
        credentials = load_scoped_credentials(
            service,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            credentials_manager=credentials_manager,
        )
        if isinstance(credentials, Mapping):
            merged_overrides.update(_filter_internal_credential_keys(credentials))
    return merged_overrides


def _create_credential_lease(
    client: httpx.Client,
    *,
    lease_url: str,
    headers: Mapping[str, str],
    tool_name: str,
    function_name: str,
    worker_scope: WorkerScope | None,
    routing_agent_name: str | None,
) -> str | None:
    credential_overrides = _collect_credential_overrides(
        tool_name,
        function_name,
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
    )
    if not credential_overrides:
        return None

    lease_payload = {
        "tool_name": tool_name,
        "function_name": function_name,
        "credential_overrides": to_json_compatible(credential_overrides),
        "ttl_seconds": _CREDENTIAL_LEASE_TTL,
        "max_uses": 1,
    }
    response = client.post(lease_url, json=lease_payload, headers=headers)
    response.raise_for_status()
    lease_data = response.json()
    if not isinstance(lease_data, Mapping) or not isinstance(lease_data.get("lease_id"), str):
        msg = "Sandbox proxy lease response is missing lease_id."
        raise TypeError(msg)
    return lease_data["lease_id"]


def _build_worker_routing_payload(
    *,
    tool_name: str,
    function_name: str,
    worker_scope: WorkerScope | None,
    routing_agent_name: str | None,
) -> tuple[dict[str, object], WorkerHandle | None]:
    if worker_scope is None:
        if not primary_worker_backend_is_dedicated():
            return {}, None

        effective_agent_name = routing_agent_name
        execution_identity = get_tool_execution_identity()
        if effective_agent_name is None and execution_identity is not None:
            effective_agent_name = execution_identity.agent_name
        if effective_agent_name is None:
            msg = (
                f"Unscoped worker-routed tool '{tool_name}.{function_name}' requires an agent name "
                "when using a dedicated worker backend."
            )
            raise RuntimeError(msg)

        worker_key = resolve_unscoped_worker_key(
            agent_name=effective_agent_name,
            execution_identity=execution_identity,
        )
        worker_handle = _get_worker_manager().ensure_worker(WorkerSpec(worker_key))
        return (
            {
                "routing_agent_name": routing_agent_name,
                "worker_key": worker_key,
            },
            worker_handle,
        )

    execution_identity = get_tool_execution_identity()
    if execution_identity is None:
        msg = f"Worker-routed tool '{tool_name}.{function_name}' requires execution identity context."
        raise RuntimeError(msg)

    worker_key = resolve_worker_key(worker_scope, execution_identity, agent_name=routing_agent_name)
    if worker_key is None:
        msg = (
            f"Worker scope '{worker_scope}' for tool '{tool_name}.{function_name}' "
            "could not be resolved from the current execution identity."
        )
        raise RuntimeError(msg)

    worker_handle = _get_worker_manager().ensure_worker(WorkerSpec(worker_key))
    return (
        {
            "worker_scope": worker_scope,
            "routing_agent_name": routing_agent_name,
            "worker_key": worker_key,
            "execution_identity": to_json_compatible(asdict(execution_identity)),
        },
        worker_handle,
    )


def _get_worker_manager() -> WorkerManager:
    return get_primary_worker_manager(proxy_url=_PROXY_URL, proxy_token=_PROXY_TOKEN)


def _request_headers_for_handle(worker_handle: WorkerHandle | None) -> dict[str, str]:
    token = worker_handle.auth_token if worker_handle is not None else _PROXY_TOKEN
    if token is None:
        msg = "MINDROOM_SANDBOX_PROXY_TOKEN must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)
    return {_SANDBOX_PROXY_TOKEN_HEADER: token}


def _current_shared_storage_root() -> Path:
    return shared_storage_root(constants.get_runtime_paths().storage_root)


def _portable_tool_init_overrides(
    tool_init_overrides: dict[str, object] | None,
    *,
    worker_key: str | None,
) -> dict[str, object] | None:
    """Rewrite storage-root absolute base_dir values only for worker-keyed requests."""
    if not tool_init_overrides:
        return tool_init_overrides
    if worker_key is None:
        return tool_init_overrides

    portable_overrides = dict(tool_init_overrides)
    raw_base_dir = portable_overrides.get("base_dir")
    if not isinstance(raw_base_dir, str):
        return portable_overrides

    base_dir = Path(raw_base_dir).expanduser()
    if not base_dir.is_absolute():
        return portable_overrides

    shared_root = _current_shared_storage_root().resolve()
    with suppress(ValueError):
        portable_overrides["base_dir"] = base_dir.resolve().relative_to(shared_root).as_posix()
    return portable_overrides


def _sandbox_proxy_enabled_for_tool(
    tool_name: str,
    *,
    worker_tools_override: list[str] | None = None,
    worker_scope: WorkerScope | None = None,
) -> bool:
    """Return whether the given tool should execute through the sandbox proxy.

    When *worker_tools_override* is not ``None``, it takes precedence over the
    env-var based ``_EXECUTION_MODE`` / ``_PROXY_TOOLS`` logic. An empty list
    means "route nothing through the proxy for this agent".
    """
    if _SANDBOX_RUNNER_MODE or tool_name in _LOCAL_ONLY_SANDBOX_TOOLS:
        return False

    if worker_tools_override is not None:
        requested = tool_name in worker_tools_override
    elif _EXECUTION_MODE in {"off", "local", "disabled"}:
        requested = False
    else:
        requested = _EXECUTION_MODE in {"all", "sandbox_all"}
        if not requested:
            requested = _PROXY_TOOLS is None or tool_name in _PROXY_TOOLS

    if not requested:
        return False

    backend_name = primary_worker_backend_name()
    if backend_name == "static_runner" and _PROXY_URL is None and worker_scope is None:
        return False

    if primary_worker_backend_available(proxy_url=_PROXY_URL, proxy_token=_PROXY_TOKEN):
        return True

    # Dedicated-worker backends must fail closed when routing is intended but the
    # provider config is incomplete; otherwise tools silently execute locally.
    return primary_worker_backend_is_dedicated()


def _call_proxy_sync(
    *,
    tool_name: str,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    tool_init_overrides: dict[str, object] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
) -> object:
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "function_name": function_name,
        "args": [to_json_compatible(arg) for arg in args],
        "kwargs": {key: to_json_compatible(value) for key, value in kwargs.items()},
    }
    worker_payload, worker_handle = _build_worker_routing_payload(
        tool_name=tool_name,
        function_name=function_name,
        worker_scope=worker_scope,
        routing_agent_name=routing_agent_name,
    )
    payload.update(worker_payload)
    if worker_handle is None and _PROXY_URL is None:
        msg = "MINDROOM_SANDBOX_PROXY_URL must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)

    try:
        headers = _request_headers_for_handle(worker_handle)
        execute_url = (
            worker_api_endpoint(worker_handle, "execute")
            if worker_handle is not None
            else (f"{_PROXY_URL}{_SANDBOX_PROXY_EXECUTE_PATH}")
        )
        worker_key = worker_payload.get("worker_key")
        portable_tool_init_overrides = _portable_tool_init_overrides(
            tool_init_overrides,
            worker_key=worker_key if isinstance(worker_key, str) else None,
        )
        if portable_tool_init_overrides:
            payload["tool_init_overrides"] = to_json_compatible(portable_tool_init_overrides)
        lease_url = (
            worker_api_endpoint(worker_handle, "leases")
            if worker_handle is not None
            else (f"{_PROXY_URL}{_SANDBOX_PROXY_LEASE_PATH}")
        )

        with httpx.Client(timeout=_PROXY_TIMEOUT) as client:
            lease_id = _create_credential_lease(
                client,
                lease_url=lease_url,
                headers=headers,
                tool_name=tool_name,
                function_name=function_name,
                worker_scope=worker_scope,
                routing_agent_name=routing_agent_name,
            )
            if lease_id is not None:
                payload["lease_id"] = lease_id

            response = client.post(execute_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        if worker_handle is not None:
            _get_worker_manager().record_failure(worker_handle.worker_key, str(exc))
        raise

    if not isinstance(data, Mapping):
        msg = "Sandbox proxy returned a non-object response."
        raise TypeError(msg)
    if data.get("ok") is True:
        if worker_handle is not None:
            _get_worker_manager().touch_worker(worker_handle.worker_key)
        return data.get("result")
    error = data.get("error") or "Sandbox execution failed."
    if worker_handle is not None:
        _get_worker_manager().record_failure(worker_handle.worker_key, str(error))
    raise RuntimeError(str(error))


def _wrap_sync_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    tool_init_overrides: dict[str, object] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return _call_proxy_sync(
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            tool_init_overrides=tool_init_overrides,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def _wrap_async_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    tool_init_overrides: dict[str, object] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    async def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return await asyncio.to_thread(
            _call_proxy_sync,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            tool_init_overrides=tool_init_overrides,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def maybe_wrap_toolkit_for_sandbox_proxy(
    tool_name: str,
    toolkit: Toolkit,
    *,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    worker_scope: WorkerScope | None = None,
    routing_agent_name: str | None = None,
) -> Toolkit:
    """Wrap toolkit functions so calls execute through the sandbox runner API.

    Note: mutates ``toolkit.functions`` and ``toolkit.async_functions`` in place.
    Callers must pass a freshly-created toolkit (``get_tool_by_name`` does this).
    """
    if not _sandbox_proxy_enabled_for_tool(
        tool_name,
        worker_tools_override=worker_tools_override,
        worker_scope=worker_scope,
    ):
        return toolkit

    toolkit.functions = {
        function_name: _wrap_sync_function(
            function,
            tool_name,
            function_name,
            tool_init_overrides=tool_init_overrides,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
        )
        for function_name, function in toolkit.functions.items()
    }
    toolkit.async_functions = {
        function_name: _wrap_async_function(
            function,
            tool_name,
            function_name,
            tool_init_overrides=tool_init_overrides,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
        )
        for function_name, function in toolkit.async_functions.items()
    }
    return toolkit
