"""Generic proxy wrapper for routing tool calls to a sandbox runner service."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from mindroom.constants import env_flag
from mindroom.credentials import get_credentials_manager

if TYPE_CHECKING:
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit

SANDBOX_PROXY_EXECUTE_PATH = "/api/sandbox-runner/execute"
SANDBOX_PROXY_LEASE_PATH = "/api/sandbox-runner/leases"
SANDBOX_PROXY_TOKEN_HEADER = "x-mindroom-sandbox-token"  # noqa: S105
DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600

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
    raw = os.getenv("MINDROOM_SANDBOX_PROXY_TIMEOUT_SECONDS", str(DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def _read_execution_mode() -> str | None:
    raw = os.getenv("MINDROOM_SANDBOX_EXECUTION_MODE")
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if not normalized:
        return None
    return normalized


def _read_credential_lease_ttl() -> int:
    raw = os.getenv("MINDROOM_SANDBOX_CREDENTIAL_LEASE_TTL_SECONDS", str(DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS))
    try:
        ttl_seconds = int(raw)
    except ValueError:
        ttl_seconds = DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


# Parsed once at module load — these don't change at runtime.
_PROXY_URL = _read_proxy_url()
_PROXY_TOKEN = _read_proxy_token()
_PROXY_TIMEOUT = _read_proxy_timeout()
_EXECUTION_MODE = _read_execution_mode()
_CREDENTIAL_LEASE_TTL = _read_credential_lease_ttl()


def sandbox_proxy_url() -> str | None:
    """Return the sandbox runner base URL, if configured."""
    return _PROXY_URL


def sandbox_proxy_token() -> str | None:
    """Return the shared token used between proxy caller and runner."""
    return _PROXY_TOKEN


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


def _parse_proxy_tools_spec(*, default: str) -> set[str] | None:
    """Parse MINDROOM_SANDBOX_PROXY_TOOLS into a set of tool names.

    Returns None when all tools should be proxied.
    """
    raw_value = os.getenv("MINDROOM_SANDBOX_PROXY_TOOLS", default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {part.strip() for part in raw_value.split(",") if part.strip()}


@functools.lru_cache(maxsize=1)
def _parse_credential_policy() -> dict[str, tuple[str, ...]]:
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


def _credential_services_for_call(tool_name: str, function_name: str) -> list[str]:
    policy = _parse_credential_policy()
    selectors = ("*", tool_name, f"{tool_name}.{function_name}")
    services: list[str] = []
    for selector in selectors:
        for service in policy.get(selector, ()):
            if service not in services:
                services.append(service)
    return services


def _filter_internal_credential_keys(credentials: Mapping[str, object]) -> dict[str, object]:
    return {str(key): value for key, value in credentials.items() if not str(key).startswith("_")}


def _collect_shared_credential_overrides(tool_name: str, function_name: str) -> dict[str, object]:
    services = _credential_services_for_call(tool_name, function_name)
    if not services:
        return {}

    credentials_manager = get_credentials_manager()
    merged_overrides: dict[str, object] = {}
    for service in services:
        credentials = credentials_manager.load_credentials(service)
        if not isinstance(credentials, Mapping):
            continue
        merged_overrides.update(_filter_internal_credential_keys(credentials))
    return merged_overrides


def sandbox_proxy_enabled_for_tool(tool_name: str) -> bool:
    """Return whether the given tool should execute through the sandbox proxy."""
    if _SANDBOX_RUNNER_MODE:
        return False

    if _PROXY_URL is None:
        return False

    if _EXECUTION_MODE in {"off", "local", "disabled"}:
        return False
    if _EXECUTION_MODE in {"all", "sandbox_all"}:
        return True
    if _EXECUTION_MODE in {"selective", "sandbox_selective"}:
        configured_tools = _parse_proxy_tools_spec(default="")
    else:
        configured_tools = _parse_proxy_tools_spec(default="*")

    return configured_tools is None or tool_name in configured_tools


def _call_proxy_sync(
    *,
    tool_name: str,
    function_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> object:
    if _PROXY_TOKEN is None:
        msg = "MINDROOM_SANDBOX_PROXY_TOKEN must be set when sandbox proxying is enabled."
        raise RuntimeError(msg)
    headers = {SANDBOX_PROXY_TOKEN_HEADER: _PROXY_TOKEN}

    # _PROXY_URL is guaranteed non-None here (checked by sandbox_proxy_enabled_for_tool).
    base_url: str = _PROXY_URL  # type: ignore[assignment]

    credential_overrides = _collect_shared_credential_overrides(tool_name, function_name)
    with httpx.Client(timeout=_PROXY_TIMEOUT) as client:
        lease_id: str | None = None
        if credential_overrides:
            lease_payload = {
                "tool_name": tool_name,
                "function_name": function_name,
                "credential_overrides": to_json_compatible(credential_overrides),
                "ttl_seconds": _CREDENTIAL_LEASE_TTL,
                "max_uses": 1,
            }
            response = client.post(f"{base_url}{SANDBOX_PROXY_LEASE_PATH}", json=lease_payload, headers=headers)
            response.raise_for_status()
            lease_data = response.json()
            if not isinstance(lease_data, Mapping) or not isinstance(lease_data.get("lease_id"), str):
                msg = "Sandbox proxy lease response is missing lease_id."
                raise RuntimeError(msg)
            lease_id = lease_data["lease_id"]

        payload: dict[str, object] = {
            "tool_name": tool_name,
            "function_name": function_name,
            "args": [to_json_compatible(arg) for arg in args],
            "kwargs": {key: to_json_compatible(value) for key, value in kwargs.items()},
        }
        if lease_id is not None:
            payload["lease_id"] = lease_id

        response = client.post(f"{base_url}{SANDBOX_PROXY_EXECUTE_PATH}", json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, Mapping):
        msg = "Sandbox proxy returned a non-object response."
        raise TypeError(msg)
    if data.get("ok") is True:
        return data.get("result")
    error = data.get("error") or "Sandbox execution failed."
    raise RuntimeError(str(error))


def _wrap_sync_function(function: Function, tool_name: str, function_name: str) -> Function:
    wrapped = function.model_copy(deep=False)

    def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return _call_proxy_sync(
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
        )

    proxy_entrypoint.__name__ = function_name
    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def _wrap_async_function(function: Function, tool_name: str, function_name: str) -> Function:
    wrapped = function.model_copy(deep=False)

    async def proxy_entrypoint(*args: object, **kwargs: object) -> object:
        return await asyncio.to_thread(
            _call_proxy_sync,
            tool_name=tool_name,
            function_name=function_name,
            args=args,
            kwargs=dict(kwargs),
        )

    proxy_entrypoint.__name__ = function_name
    wrapped.entrypoint = proxy_entrypoint
    return wrapped


def maybe_wrap_toolkit_for_sandbox_proxy(tool_name: str, toolkit: Toolkit) -> Toolkit:
    """Wrap toolkit functions so calls execute through the sandbox runner API.

    Note: mutates ``toolkit.functions`` and ``toolkit.async_functions`` in place.
    Callers must pass a freshly-created toolkit (``get_tool_by_name`` does this).
    """
    if not sandbox_proxy_enabled_for_tool(tool_name):
        return toolkit

    toolkit.functions = {
        function_name: _wrap_sync_function(function, tool_name, function_name)
        for function_name, function in toolkit.functions.items()
    }
    toolkit.async_functions = {
        function_name: _wrap_async_function(function, tool_name, function_name)
        for function_name, function in toolkit.async_functions.items()
    }
    return toolkit
