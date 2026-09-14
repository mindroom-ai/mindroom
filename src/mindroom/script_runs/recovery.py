"""Versioned authority contract for adopting an existing background script process."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from mindroom.script_runs.compatibility import SCRIPT_PROTOCOL_VERSION

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.workers.backend import WorkerBackend


@runtime_checkable
class _RecoveringScriptBackend(Protocol):
    def script_recovery_signature(self) -> str:
        """Return a digest of the authority preserved by an existing worker."""


def script_recovery_signature(
    *,
    backend: WorkerBackend | None,
    config: Config,
    agent_name: str,
    gateway_url: str,
) -> str | None:
    """Bind one recoverable launch to its worker authority, private scope and gateway."""
    if (
        backend is None
        or backend.backend_name != "kubernetes"
        or not isinstance(backend, _RecoveringScriptBackend)
        or not gateway_url
        or agent_name not in config.agents
        or not any(tool.name == "script" for tool in config.resolve_entity(agent_name).tool_configs)
    ):
        return None
    private = config.agents[agent_name].private
    payload = {
        "protocol": SCRIPT_PROTOCOL_VERSION,
        "backend": backend.script_recovery_signature(),
        "agent": agent_name,
        "private": None if private is None else private.model_dump(mode="json"),
        "gateway": gateway_url.rstrip("/"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
