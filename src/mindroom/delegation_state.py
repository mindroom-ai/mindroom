"""Serializable runtime ownership for fresh delegated child runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

DELEGATION_STATE_KEY = "mindroom_delegation"


@dataclass
class DelegationChild:
    """One parent requirement and its stable child execution identity."""

    delegation_id: str
    parent_tool_call_id: str
    caller_agent_name: str
    child_agent_name: str
    task: str
    session_id: str
    run_id: str
    model_name: str
    depth: int
    execution_identity: dict[str, object]
    parent_requirement_id: str = ""
    storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    record_locator: dict[str, object] = field(default_factory=dict)
    status: Literal["running", "paused", "completed", "failed", "cancelled", "denied"] = "running"
    result: str | None = None


@dataclass
class DelegationState:
    """Persisted parent waits; workspace exports never own this state."""

    children: list[DelegationChild] = field(default_factory=list)
    gates: dict[str, bool] = field(default_factory=dict)
    pending_tools: list[dict[str, Any]] = field(default_factory=list)
    pending_requirements: list[dict[str, Any]] = field(default_factory=list)
    pending_agent_name: str | None = None
    pending_child_id: str | None = None
    storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return an independent JSON-compatible snapshot."""
        return asdict(self)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> DelegationState:
        """Read the driver-owned snapshot attached to an Agno run."""
        stored = (metadata or {}).get(DELEGATION_STATE_KEY)
        if stored is None:
            return cls()
        if not isinstance(stored, dict):
            msg = "Invalid delegation runtime state"
            raise TypeError(msg)
        snapshot = cast("dict[str, Any]", stored)
        return cls(
            children=[DelegationChild(**child) for child in snapshot.get("children", [])],
            gates=dict(snapshot.get("gates", {})),
            pending_tools=list(snapshot.get("pending_tools", [])),
            pending_requirements=list(snapshot.get("pending_requirements", [])),
            pending_agent_name=snapshot.get("pending_agent_name"),
            pending_child_id=snapshot.get("pending_child_id"),
            storage_bindings=dict(snapshot.get("storage_bindings", {})),
        )

    def clear_pending(self) -> None:
        """Clear only the presented approval generation, retaining sibling results."""
        self.pending_tools = []
        self.pending_requirements = []
        self.pending_agent_name = None
        self.pending_child_id = None
