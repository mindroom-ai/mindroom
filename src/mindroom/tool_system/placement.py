"""Sandbox settings and tool placement decisions for one runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING

from mindroom.config.tool_aliases import canonical_tool_name
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind

if TYPE_CHECKING:
    from collections.abc import Collection

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.registry import ToolRegistry

__all__ = [
    "SandboxProxyConfig",
    "ToolPlacement",
    "primary_owns_tool_settings",
    "sandbox_proxy_config",
    "tool_placement",
]

_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS = 120.0
_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS = 60
_MAX_CREDENTIAL_LEASE_TTL_SECONDS = 3600
_SANDBOX_ALL_EXECUTION_MODES = frozenset({"all", "sandbox_all"})
_SANDBOX_SELECTIVE_EXECUTION_MODES = frozenset({"selective", "sandbox_selective"})
_UNSAFE_LOCAL_EXECUTION_MODES = frozenset({"off", "local", "disabled"})


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
    value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_url"])
    return None if value is None else value.strip().rstrip("/")


def _read_proxy_token(runtime_paths: RuntimePaths) -> str | None:
    value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_token"])
    return None if value is None else value.strip()


def _read_proxy_timeout(runtime_paths: RuntimePaths) -> float:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["proxy_timeout_seconds"],
        default=str(_DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS),
    )
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_SANDBOX_PROXY_TIMEOUT_SECONDS


def _read_execution_mode(runtime_paths: RuntimePaths) -> str | None:
    raw = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["execution_mode"])
    normalized = (raw or "").strip().lower()
    if not normalized:
        return None
    supported = _SANDBOX_ALL_EXECUTION_MODES | _SANDBOX_SELECTIVE_EXECUTION_MODES | _UNSAFE_LOCAL_EXECUTION_MODES
    if normalized not in supported:
        msg = f"Invalid MINDROOM_SANDBOX_EXECUTION_MODE {raw!r}; expected one of {', '.join(sorted(supported))}"
        raise ValueError(msg)
    return normalized


def _read_credential_lease_ttl(runtime_paths: RuntimePaths) -> int:
    raw = runtime_paths.env_value(
        SANDBOX_RUNTIME_ENV_BY_KEY["credential_lease_ttl_seconds"],
        default=str(_DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS),
    )
    try:
        ttl_seconds = int(raw)
    except ValueError:
        ttl_seconds = _DEFAULT_CREDENTIAL_LEASE_TTL_SECONDS
    return max(1, min(_MAX_CREDENTIAL_LEASE_TTL_SECONDS, ttl_seconds))


def _read_proxy_tools(
    runtime_paths: RuntimePaths,
    execution_mode: str | None,
    *,
    proxy_url: str | None,
) -> set[str] | None:
    default = (
        "*"
        if execution_mode in _SANDBOX_ALL_EXECUTION_MODES
        or (
            execution_mode is None
            and proxy_url is not None
            and worker_backend_kind(runtime_paths) is WorkerBackendKind.STATIC_RUNNER
        )
        else ""
    )
    raw_value = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_tools"], default=default).strip()
    if raw_value == "*":
        return None
    if not raw_value:
        return set()
    return {canonical_tool_name(part.strip()) for part in raw_value.split(",") if part.strip()}


def _read_credential_policy(runtime_paths: RuntimePaths) -> dict[str, tuple[str, ...]]:
    raw_policy = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["credential_policy_json"])
    if raw_policy is None:
        return {}

    try:
        parsed = json.loads(raw_policy)
    except json.JSONDecodeError:
        return {}

    if not isinstance(parsed, dict):
        return {}

    return {
        selector.strip(): tuple(service.strip() for service in services if isinstance(service, str) and service.strip())
        for selector, services in parsed.items()
        if isinstance(selector, str) and isinstance(services, list)
    }


def sandbox_proxy_config(runtime_paths: RuntimePaths) -> SandboxProxyConfig:
    """Return sandbox proxy settings for one explicit runtime context."""
    execution_mode = _read_execution_mode(runtime_paths)
    proxy_url = _read_proxy_url(runtime_paths)
    return SandboxProxyConfig(
        runner_mode=runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]),
        proxy_url=proxy_url,
        proxy_token=_read_proxy_token(runtime_paths),
        proxy_timeout_seconds=_read_proxy_timeout(runtime_paths),
        execution_mode=execution_mode,
        credential_lease_ttl_seconds=_read_credential_lease_ttl(runtime_paths),
        proxy_tools=_read_proxy_tools(runtime_paths, execution_mode, proxy_url=proxy_url),
        credential_policy=_read_credential_policy(runtime_paths),
    )


def _tool_defaults_to_worker(metadata: ToolMetadata | None) -> bool:
    return metadata is not None and metadata.default_execution_target.value == "worker"


def _sandbox_proxy_requested_for_tool(
    *,
    tool_name: str,
    metadata: ToolMetadata | None,
    proxy_config: SandboxProxyConfig,
    worker_tools: Collection[str] | None,
) -> bool:
    """Return whether config requests worker/proxy routing for one tool."""
    if worker_tools is not None:
        requested = tool_name in worker_tools
    elif proxy_config.execution_mode in _UNSAFE_LOCAL_EXECUTION_MODES:
        requested = False
    elif proxy_config.execution_mode in _SANDBOX_ALL_EXECUTION_MODES:
        requested = True
    elif proxy_config.execution_mode in _SANDBOX_SELECTIVE_EXECUTION_MODES:
        requested = proxy_config.proxy_tools is None or tool_name in proxy_config.proxy_tools
    elif proxy_config.execution_mode is not None:
        requested = False
    elif proxy_config.proxy_tools is None:
        requested = True
    elif proxy_config.proxy_tools:
        requested = tool_name in proxy_config.proxy_tools
    else:
        requested = _tool_defaults_to_worker(metadata)
    return requested


@dataclass(frozen=True)
class ToolPlacement:
    """The sandbox settings of one runtime, which decide where each tool of a registry runs: primary or worker.

    Settings are parsed once; the worker backend is checked only when a tool's placement depends on it.
    """

    runtime_paths: RuntimePaths
    proxy_config: SandboxProxyConfig
    tools: ToolRegistry

    @cached_property
    def _backend_kind(self) -> WorkerBackendKind:
        return worker_backend_kind(self.runtime_paths)

    def runs_in_worker(self, tool_name: str, worker_tools: Collection[str] | None) -> bool:
        """Return whether calls of ``tool_name`` run in a worker for an agent with these ``worker_tools``.

        ``worker_tools`` is the agent's resolved list, which takes precedence over the env-var based
        execution mode and proxy tools; None follows them, and an empty list routes nothing.
        """
        proxy_config = self.proxy_config
        metadata = self.tools.get(tool_name)
        if proxy_config.runner_mode or (
            metadata is not None and (metadata.requires_primary_runtime or metadata.requires_room_context)
        ):
            return False
        if not _sandbox_proxy_requested_for_tool(
            tool_name=tool_name,
            metadata=metadata,
            proxy_config=proxy_config,
            worker_tools=worker_tools,
        ):
            return False
        if (
            worker_tools is None
            and proxy_config.execution_mode is None
            and proxy_config.proxy_tools is not None
            and not proxy_config.proxy_tools
            and proxy_config.proxy_url is None
            and self._backend_kind is WorkerBackendKind.STATIC_RUNNER
        ):
            return False
        # A dedicated backend routes even when its config is incomplete, so the call fails instead of running here.
        if self._backend_kind.dedicated or proxy_config.proxy_url:
            return True
        # Execution tools also fail closed on the default static backend. To allow
        # host execution, operators must opt in with MINDROOM_SANDBOX_EXECUTION_MODE
        # set to off/local/disabled or MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS=true.
        return not (
            _tool_defaults_to_worker(metadata)
            and self.runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["unsafe_allow_local_execution_tools"])
        )


def tool_placement(runtime_paths: RuntimePaths, tools: ToolRegistry) -> ToolPlacement:
    """Return where each tool of ``tools`` runs in one runtime."""
    return ToolPlacement(runtime_paths=runtime_paths, proxy_config=sandbox_proxy_config(runtime_paths), tools=tools)


def primary_owns_tool_settings(tool_name: str, tools: ToolRegistry, *, runtime_paths: RuntimePaths) -> bool:
    """Return whether this primary process owns the settings of a tool in ``tools``.

    The primary builds every tool, including tools whose calls run in a worker, so a worker-writable store never
    configures it; routed calls receive the settings through a per-call lease instead.
    """
    return tool_name in tools and not sandbox_proxy_config(runtime_paths).runner_mode
