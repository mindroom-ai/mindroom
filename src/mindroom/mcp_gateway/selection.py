"""One durable agent selection per verified owner, shared by all MCP clients."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from mindroom.mcp_gateway.accounts import account_is_active
from mindroom.mcp_gateway.types import GATEWAY_AGENT_NAME_LIMIT, GatewayError, GatewayErrorCode

if TYPE_CHECKING:
    import sqlite3

    from mindroom.mcp_gateway.store import GatewayOAuthStore
    from mindroom.mcp_gateway.types import GatewayOwner


class SelectionAccessDeniedError(GatewayError):
    """The saved selection or provisioned account no longer allows this call."""

    def __init__(self) -> None:
        super().__init__(GatewayErrorCode.UNAUTHORIZED)


def _owner_key(owner: GatewayOwner) -> str:
    identity = json.dumps([owner.authenticated_user_id, owner.requester_id, owner.account_id])
    return hashlib.sha256(identity.encode()).hexdigest()


def _payload(names: tuple[str, ...]) -> str:
    if (
        len(names) > 1000
        or len(set(names)) != len(names)
        or any(not name or len(name) > GATEWAY_AGENT_NAME_LIMIT for name in names)
    ):
        msg = "Agent selection requires bounded unique names"
        raise ValueError(msg)
    payload = json.dumps(names)
    if len(payload.encode()) > 65_536:
        msg = "Agent selection is too large"
        raise ValueError(msg)
    return payload


class GatewaySelections:
    """Selections grant no agent authority; callers also resolve current eligibility."""

    def __init__(self, store: GatewayOAuthStore) -> None:
        self.store = store

    @staticmethod
    def _read(connection: sqlite3.Connection, owner: GatewayOwner) -> tuple[str, ...] | None:
        if owner.account_id is not None and not account_is_active(connection, owner.account_id):
            raise SelectionAccessDeniedError
        row = connection.execute(
            "SELECT agents FROM gateway_selections WHERE owner_key = ?",
            (_owner_key(owner),),
        ).fetchone()
        return tuple(json.loads(row["agents"])) if row else None

    def _save(self, connection: sqlite3.Connection, owner: GatewayOwner, payload: str) -> tuple[str, ...]:
        previous = self._read(connection, owner)
        names = tuple(json.loads(payload))
        connection.execute(
            """INSERT INTO gateway_selections (owner_key, requester_id, account_id, agents) VALUES (?, ?, ?, ?)
               ON CONFLICT(owner_key) DO UPDATE SET agents = excluded.agents""",
            (_owner_key(owner), owner.requester_id, owner.account_id, payload),
        )
        # Withdrawing exposure must remain possible after an operator lowers the quotas.
        if previous is None or not set(names).issubset(previous):
            self.store.require_capacity(connection, requester_id=owner.requester_id)
        return names

    async def get(self, owner: GatewayOwner, default_agents: tuple[str, ...]) -> tuple[str, ...]:
        """Persist the initial default once, without overwriting a deliberate empty selection."""
        saved = await self.store.read(lambda connection: self._read(connection, owner))
        if saved is not None:
            return saved
        payload = _payload(default_agents)

        def initialize(connection: sqlite3.Connection) -> tuple[str, ...]:
            current = self._read(connection, owner)
            return current if current is not None else self._save(connection, owner, payload)

        return await self.store.transact(initialize)

    async def set(self, owner: GatewayOwner, names: tuple[str, ...]) -> tuple[str, ...]:
        """Replace the complete selection atomically under the existing storage budgets."""
        payload = _payload(names)
        return await self.store.transact(lambda connection: self._save(connection, owner, payload))

    def require_selected(self, owner: GatewayOwner, agent_name: str) -> None:
        """Read committed authority immediately before dispatch; safe to call in worker threads."""
        selected = self.store.read_sync(lambda connection: self._read(connection, owner))
        if selected is None or agent_name not in selected:
            raise SelectionAccessDeniedError
