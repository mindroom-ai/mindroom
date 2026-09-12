"""Value helpers for bounded thread tool approval grants."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.mcp.config import resolved_mcp_tool_prefix

if TYPE_CHECKING:
    from mindroom.config.main import Config

AUTO_APPROVE_OPTIONS = (300, 600, 1800)


def valid_auto_approve_seconds(value: object) -> bool:
    """Accept only explicitly supported integer durations."""
    return type(value) is int and value in AUTO_APPROVE_OPTIONS


def approval_binding(config: Config) -> str:
    """Fence grants when authored tool, agent, or provider bindings change."""
    return hashlib.sha256(config.model_dump_json().encode()).hexdigest()


def grant_operation(config: Config, tool_name: str, arguments: dict[str, object]) -> str | None:
    """Bind a concrete tool operation, including generic MCP dispatch targets."""
    operation: list[str] = [tool_name]
    for server_id, server in config.mcp_servers.items():
        prefix = resolved_mcp_tool_prefix(server_id, server)
        if tool_name == f"{prefix}_call_tool":
            remote = arguments.get("tool_name")
            if not isinstance(remote, str) or not remote.strip():
                return None
            operation.extend((server_id, remote))
            break
    return approval_binding(config) + ":" + json.dumps(operation, separators=(",", ":"))


def approval_timestamp(timestamp_ns: int) -> str:
    """Render one fixed journal deadline on the Matrix wire."""
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """Durable grant identity retained independently of retired approval cards."""

    grant_id: str
    room_id: str
    thread_id: str
    card_event_id: str
    requester_id: str
    entity_name: str
    invoking_agent: str
    operation: str
    expires_at_ns: int
    revoked_at_ns: int | None

    def wire(self) -> dict[str, object]:
        """Return the authoritative grant acknowledgement carried by a card edit."""
        return {
            "grant_id": self.grant_id,
            "expires_at": approval_timestamp(self.expires_at_ns),
            "revoked_at": None if self.revoked_at_ns is None else approval_timestamp(self.revoked_at_ns),
        }
