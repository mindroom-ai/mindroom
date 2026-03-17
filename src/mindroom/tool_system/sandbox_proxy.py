"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import execution_runtime_env_values
from mindroom.credentials import load_scoped_credentials
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.worker_routing import (
    SHARED_ONLY_INTEGRATION_NAMES,
    ResolvedWorkerTarget,
    WorkerScope,
    resolve_unscoped_worker_key,
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

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.workers.manager import WorkerManager

_SANDBOX_PROXY_EXECUTE_PATH = "/api/sandbox-runner/execute"
_SANDBOX_PROXY_LEASE_PATH = "/api/sandbox-runner/leases"
_SANDBOX_PROXY_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
_LOCAL_ONLY_SANDBOX_TOOLS = frozenset(SHARED_ONLY_INTEGRATION_NAMES - {"google", "spotify"})
_EXECUTION_ENV_TOOL_NAMES = frozenset({"python", "shell"})


@dataclass(frozen=True)
class SandboxProxyConfig:
    """Resolved sandbox proxy settings for one explicit runtime context."""

    runner_mode: bool
    proxy_url: str | None
    proxy_token: str | None
    proxy_timeout_seconds: float
    execution_mode: str | None
    credential_lease_ttl_seconds: int
    proxy_tools: set[str] | None
    credential_policy: dict[str, tuple[str, ...]]


def _read_proxy_url(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_URL", default="") or "").strip()
    if not value:
        return None
    return value.rstrip("/")


def _read_proxy_token(runtime_paths: RuntimePaths) -> str | None:
    value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOKEN", default="") or "").strip()
    if not value:
        return None
    return value


def _read_proxy_timeout(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(
        "MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS",
        default=str(_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS),
    )
    try:
        return float(raw or _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS)
    except ValueError:
        return _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def _read_execution_mode(runtime_paths: RuntimePaths) -> str | None:
    raw = runtime_paths.env_value("MINDROOM_SANDBOX_EXECUTION_MODE")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    return normalized


def _read_credential_lease_ttl(runtime_paths: RuntimePaths) -> int:
    raw = runtime_paths.env_value(
        "MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS",
        default=str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS),
    )
    try:
        ttl_seconds = int(raw or _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(runtime_paths: RuntimePaths, execution_mode: str | None) -> set[str] | None:
    default = "" if execution_mode in {"selective", "sandbox_selective"} else "*"
    raw_value = (runtime_paths.env_value("MINDROOM_SANDBOX_PROXY_TOOLS", default=default) or default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


def _read_credential_policy(runtime_paths: RuntimePaths) -> dict[str, tuple[str, ...]]:
    raw_policy = (runtime_paths.env_value("MINDROOM_SANDBOX_CREDENTIAL_POLICY_JSON", default="") or "").strip()
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


def sandbox_proxy_config(runtime_paths: RuntimePaths) -> SandboxProxyConfig:
    """Return sandbox proxy settings for one explicit runtime context."""
    execution_mode = _read_execution_mode(runtime_paths)
    return SandboxProxyConfig(
        runner_mode=runtime_paths.env_flag("MINDROOM_SANDBOX_RUNNER_MODE"),
        proxy_url=_read_proxy_url(runtime_paths),
        proxy_token=_read_proxy_token(runtime_paths),
        proxy_timeout_seconds=_read_proxy_timeout(runtime_paths),
        execution_mode=execution_mode,
        credential_lease_ttl_seconds=_read_credential_lease_ttl(runtime_paths),
        proxy_tools=_read_proxy_tools(runtime_paths, execution_mode),
        credential_policy=_read_credential_policy(runtime_paths),
    )


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


def _credential_services_for_call(
    tool_name: str,
    function_name: str,
    *,
    proxy_config: SandboxProxyConfig,
) -> list[str]:
    policy = proxy_config.credential_policy
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
    proxy_config: SandboxProxyConfig,
    credentials_manager: CredentialsManager | None,
    worker_target: ResolvedWorkerTarget | None,
) -> dict[str, object]:
    if credentials_manager is None:
        return {}
    services = _credential_services_for_call(tool_name, function_name, proxy_config=proxy_config)
    if not services:
        return {}

    merged_overrides: dict[str, object] = {}
    for service in services:
        credentials = load_scoped_credentials(
            service,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        if isinstance(credentials, Mapping):
            merged_overrides.update(_filter_internal_credential_keys(credentials))
    return merged_overrides


def _create_credential_lease(
    client: httpx.Client,
    *,
    proxy_config: SandboxProxyConfig,
    lease_url: str,
    headers: Mapping[str, str],
    credentials_manager: CredentialsManager | None,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
) -> str | None:
    credential_overrides = _collect_credential_overrides(
        tool_name,
        function_name,
        proxy_config=proxy_config,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
    )
    if not credential_overrides:
        return None

    lease_payload = {
        "tool_name": tool_name,
        "function_name": function_name,
        "credential_overrides": to_json_compatible(credential_overrides),
        "ttl_seconds": proxy_config.credential_lease_ttl_seconds,
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
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget | None,
) -> tuple[dict[str, object], WorkerHandle | None]:
    proxy_config = sandbox_proxy_config(runtime_paths)
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    execution_identity = worker_target.execution_identity if worker_target is not None else None
    routing_agent_name = worker_target.routing_agent_name if worker_target is not None else None
    if worker_scope is None:
        if not primary_worker_backend_is_dedicated(runtime_paths):
            return {}, None

        effective_agent_name = routing_agent_name
        if effective_agent_name is None:
            msg = (
                f"Unscoped worker-routed tool '{tool_name}.{function_name}' requires an agent name "
                "when using a dedicated worker backend."
            )
            raise RuntimeError(msg)

        worker_key = resolve_unscoped_worker_key(
            agent_name=effective_agent_name,
            execution_identity=execution_identity,
            tenant_id=worker_target.tenant_id if worker_target is not None else None,
            account_id=worker_target.account_id if worker_target is not None else None,
        )
        worker_handle = _get_worker_manager(runtime_paths, proxy_config).ensure_worker(WorkerSpec(worker_key))
        return (
            {
                "routing_agent_name": effective_agent_name,
                "worker_key": worker_key,
            },
            worker_handle,
        )

    if worker_target is None or execution_identity is None:
        msg = f"Worker-routed tool '{tool_name}.{function_name}' requires execution identity context."
        raise RuntimeError(msg)

    effective_agent_name = routing_agent_name
    if worker_scope == "user_agent":
        worker_key, resolved_private_agent_names = _resolve_user_agent_worker_payload(
            tool_name=tool_name,
            function_name=function_name,
            worker_target=worker_target,
        )
    else:
        resolved_private_agent_names = None
        worker_key = worker_target.worker_key
        if worker_key is None:
            msg = (
                f"Worker scope '{worker_scope}' for tool '{tool_name}.{function_name}' "
                "could not be resolved from the current execution identity."
            )
            raise RuntimeError(msg)
    worker_handle = _get_worker_manager(runtime_paths, proxy_config).ensure_worker(
        WorkerSpec(worker_key, private_agent_names=resolved_private_agent_names),
    )
    return (
        {
            "worker_scope": worker_scope,
            "routing_agent_name": effective_agent_name,
            "worker_key": worker_key,
            "execution_identity": to_json_compatible(asdict(execution_identity)),
            "private_agent_names": (
                sorted(resolved_private_agent_names) if resolved_private_agent_names is not None else None
            ),
        },
        worker_handle,
    )


def _resolve_user_agent_worker_payload(
    *,
    tool_name: str,
    function_name: str,
    worker_target: ResolvedWorkerTarget,
) -> tuple[str, frozenset[str]]:
    if worker_target.private_agent_names is None:
        msg = (
            f"Worker-routed tool '{tool_name}.{function_name}' with scope 'user_agent' "
            "requires explicit private visibility."
        )
        raise RuntimeError(msg)
    worker_key = worker_target.worker_key
    if worker_key is None:
        msg = (
            f"Worker scope 'user_agent' for tool '{tool_name}.{function_name}' "
            "could not be resolved from the current execution identity."
        )
        raise RuntimeError(msg)
    return worker_key, worker_target.private_agent_names


def _get_worker_manager(
    runtime_paths: RuntimePaths,
    proxy_config: SandboxProxyConfig,
) -> WorkerManager:
    context = get_tool_runtime_context()
    storage_root = (
        context.storage_path if context is not None and context.storage_path is not None else runtime_paths.storage_root
    )
    return get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=storage_root,
    )


def _execution_env_payload(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
) -> dict[str, str] | None:
    """Return explicit execution env only for tools that intentionally support it."""
    if tool_name not in _EXECUTION_ENV_TOOL_NAMES:
        return None
    return dict(execution_runtime_env_values(runtime_paths))


def _request_headers_for_handle(
    worker_handle: WorkerHandle | None,
    *,
    proxy_config: SandboxProxyConfig,
) -> dict[str, str]:
    token = worker_handle.auth_token if worker_handle is not None else proxy_config.proxy_token
    if token is None:
        msg = "MINDROOM_SANDBOX_PROXY_TOKEN must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)
    return {_SANDBOX_PROXY_TOKEN_HEADER: token}


def _portable_tool_init_overrides(
    tool_init_overrides: dict[str, object] | None,
    *,
    shared_storage_root_path: Path | None,
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

    if shared_storage_root_path is None:
        return portable_overrides

    shared_root = shared_storage_root_path.resolve()
    with suppress(ValueError):
        portable_overrides["base_dir"] = base_dir.resolve().relative_to(shared_root).as_posix()
    return portable_overrides


def _sandbox_proxy_enabled_for_tool(
    tool_name: str,
    *,
    runtime_paths: RuntimePaths,
    worker_tools_override: list[str] | None = None,
    worker_scope: WorkerScope | None = None,
) -> bool:
    """Return whether the given tool should execute through the sandbox proxy.

    When *worker_tools_override* is not ``None``, it takes precedence over the
    env-var based ``_EXECUTION_MODE`` / ``_PROXY_TOOLS`` logic. An empty list
    means "route nothing through the proxy for this agent".
    """
    proxy_config = sandbox_proxy_config(runtime_paths)
    if proxy_config.runner_mode or tool_name in _LOCAL_ONLY_SANDBOX_TOOLS:
        return False

    if worker_tools_override is not None:
        requested = tool_name in worker_tools_override
    elif proxy_config.execution_mode in {"off", "local", "disabled"}:
        requested = False
    else:
        requested = proxy_config.execution_mode in {"all", "sandbox_all"}
        if not requested:
            requested = proxy_config.proxy_tools is None or tool_name in proxy_config.proxy_tools

    if not requested:
        return False

    backend_name = primary_worker_backend_name(runtime_paths)
    if backend_name == "static_runner" and proxy_config.proxy_url is None and worker_scope is None:
        return False

    if primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        return True

    # Dedicated-worker backends must fail closed when routing is intended but the
    # provider config is incomplete; otherwise tools silently execute locally.
    return primary_worker_backend_is_dedicated(runtime_paths)


def _call_proxy_sync(  # noqa: C901
    *,
    runtime_paths: RuntimePaths,
    tool_name: str,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> object:
    proxy_config = sandbox_proxy_config(runtime_paths)
    payload: dict[str, object] = {
        "tool_name": tool_name,
        "function_name": function_name,
        "args": [to_json_compatible(arg) for arg in args],
        "kwargs": {key: to_json_compatible(value) for key, value in kwargs.items()},
    }
    worker_payload, worker_handle = _build_worker_routing_payload(
        runtime_paths=runtime_paths,
        tool_name=tool_name,
        function_name=function_name,
        worker_target=worker_target,
    )
    payload.update(worker_payload)
    if execution_env := _execution_env_payload(tool_name, runtime_paths=runtime_paths):
        payload["execution_env"] = execution_env
    if worker_handle is None and proxy_config.proxy_url is None:
        msg = "MINDROOM_SANDBOX_PROXY_URL must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)

    try:
        headers = _request_headers_for_handle(worker_handle, proxy_config=proxy_config)
        execute_url = (
            worker_api_endpoint(worker_handle, "execute")
            if worker_handle is not None
            else (f"{proxy_config.proxy_url}{_SANDBOX_PROXY_EXECUTE_PATH}")
        )
        worker_key = worker_payload.get("worker_key")
        portable_tool_init_overrides = _portable_tool_init_overrides(
            tool_init_overrides,
            shared_storage_root_path=shared_storage_root_path,
            worker_key=worker_key if isinstance(worker_key, str) else None,
        )
        if portable_tool_init_overrides:
            payload["tool_init_overrides"] = to_json_compatible(portable_tool_init_overrides)
        lease_url = (
            worker_api_endpoint(worker_handle, "leases")
            if worker_handle is not None
            else (f"{proxy_config.proxy_url}{_SANDBOX_PROXY_LEASE_PATH}")
        )

        with httpx.Client(timeout=proxy_config.proxy_timeout_seconds) as client:
            lease_id = _create_credential_lease(
                client,
                proxy_config=proxy_config,
                lease_url=lease_url,
                headers=headers,
                credentials_manager=credentials_manager,
                tool_name=tool_name,
                function_name=function_name,
                worker_target=worker_target,
            )
            if lease_id is not None:
                payload["lease_id"] = lease_id

            response = client.post(execute_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        if worker_handle is not None:
            _get_worker_manager(runtime_paths, proxy_config).record_failure(worker_handle.worker_key, str(exc))
        raise

    if not isinstance(data, Mapping):
        msg = "Sandbox proxy returned a non-object response."
        raise TypeError(msg)
    if data.get("ok") is True:
        if worker_handle is not None:
            _get_worker_manager(runtime_paths, proxy_config).touch_worker(worker_handle.worker_key)
        return data.get("result")
    error = data.get("error") or "Sandbox execution failed."
    if worker_handle is not None:
        _get_worker_manager(runtime_paths, proxy_config).record_failure(worker_handle.worker_key, str(error))
    raise RuntimeError(str(error))


def _wrap_sync_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return _call_proxy_sync(
            runtime_paths=runtime_paths,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_init_overrides=tool_init_overrides,
            worker_target=worker_target,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def _wrap_async_function(
    function: Function,
    tool_name: str,
    function_name: str,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Function:
    wrapped = function.model_copy(deep=False)
    assert function.entrypoint is not None

    @functools.wraps(function.entrypoint)
    async def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return await asyncio.to_thread(
            _call_proxy_sync,
            runtime_paths=runtime_paths,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_init_overrides=tool_init_overrides,
            worker_target=worker_target,
        )

    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def maybe_wrap_toolkit_for_sandbox_proxy(
    tool_name: str,
    toolkit: Toolkit,
    *,
    runtime_paths: RuntimePaths,
    credentials_manager: CredentialsManager | None,
    shared_storage_root_path: Path | None = None,
    tool_init_overrides: dict[str, object] | None = None,
    worker_tools_override: list[str] | None = None,
    worker_target: ResolvedWorkerTarget | None = None,
) -> Toolkit:
    """Wrap toolkit functions so calls execute through the sandbox runner API.

    Note: mutates ``toolkit.functions`` and ``toolkit.async_functions`` in place.
    Callers must pass a freshly-created toolkit (``get_tool_by_name`` does this).
    """
    if not _sandbox_proxy_enabled_for_tool(
        tool_name,
        runtime_paths=runtime_paths,
        worker_tools_override=worker_tools_override,
        worker_scope=worker_target.worker_scope if worker_target is not None else None,
    ):
        return toolkit

    toolkit.functions = {
        function_name: _wrap_sync_function(
            function,
            tool_name,
            function_name,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_init_overrides=tool_init_overrides,
            worker_target=worker_target,
        )
        for function_name, function in toolkit.functions.items()
    }
    toolkit.async_functions = {
        function_name: _wrap_async_function(
            function,
            tool_name,
            function_name,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            shared_storage_root_path=shared_storage_root_path,
            tool_init_overrides=tool_init_overrides,
            worker_target=worker_target,
        )
        for function_name, function in toolkit.async_functions.items()
    }
    return toolkit
