"""Unified Matrix ID handling system."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, ClassVar

from mindroom.constants import MATRIX_SERVER_NAME, MINDROOM_NAMESPACE, ROUTER_AGENT_NAME

if TYPE_CHECKING:
    from mindroom.config.main import Config

_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]{4,32}$")


def _normalize_namespace(namespace: str | None) -> str | None:
    """Normalize and validate an installation namespace."""
    if namespace is None:
        return None
    normalized = namespace.strip().lower()
    if not normalized:
        return None
    if not _NAMESPACE_PATTERN.fullmatch(normalized):
        msg = f"MINDROOM_NAMESPACE must match ^[a-z0-9]{{4,32}}$ (got: {namespace!r})"
        raise ValueError(msg)
    return normalized


_ACTIVE_NAMESPACE = _normalize_namespace(MINDROOM_NAMESPACE)


def mindroom_namespace() -> str | None:
    """Return the configured installation namespace, if any."""
    return _ACTIVE_NAMESPACE


def _strip_agent_namespace_suffix(agent_identifier: str) -> str | None:
    """Return the agent name without namespace suffix, or None if namespace mismatches."""
    namespace = mindroom_namespace()
    if not namespace:
        return agent_identifier

    suffix = f"_{namespace}"
    if not agent_identifier.endswith(suffix):
        return None

    stripped = agent_identifier[: -len(suffix)]
    return stripped or None


def managed_room_alias_localpart(room_key: str) -> str:
    """Build the managed room alias localpart for a room key."""
    namespace = mindroom_namespace()
    if not namespace:
        return room_key
    return f"{room_key}_{namespace}"


def managed_room_key_from_alias_localpart(alias_localpart: str) -> str | None:
    """Extract the configured managed room key from an alias localpart."""
    namespace = mindroom_namespace()
    if not namespace:
        return alias_localpart

    suffix = f"_{namespace}"
    if not alias_localpart.endswith(suffix):
        return None
    room_key = alias_localpart[: -len(suffix)]
    return room_key or None


@dataclass(frozen=True)
class MatrixID:
    """Immutable Matrix ID representation with parsing and validation."""

    username: str
    domain: str

    AGENT_PREFIX: ClassVar[str] = "mindroom_"

    @classmethod
    def parse(cls, matrix_id: str) -> MatrixID:
        """Parse a Matrix ID like @mindroom_calculator:localhost."""
        return _parse_matrix_id(matrix_id)

    @classmethod
    def from_agent(cls, agent_name: str, domain: str) -> MatrixID:
        """Create a MatrixID for an agent."""
        return cls(username=agent_username_localpart(agent_name), domain=domain)

    @classmethod
    def from_username(cls, username: str, domain: str) -> MatrixID:
        """Create a MatrixID from a username (without @ prefix)."""
        return cls(username=username, domain=domain)

    @property
    def full_id(self) -> str:
        """Get the full Matrix ID like @mindroom_calculator:localhost."""
        return f"@{self.username}:{self.domain}"

    def agent_name(self, config: Config) -> str | None:
        """Extract agent name if this is a configured agent ID.

        Only IDs whose domain matches ``config.domain`` are recognised.
        A cross-domain ID like ``@mindroom_assistant:evil.com`` is never
        treated as a local agent, even if the localpart matches.
        """
        if self.domain != config.domain or not self.username.startswith(self.AGENT_PREFIX):
            return None

        # Remove prefix
        agent_identifier = self.username[len(self.AGENT_PREFIX) :]
        name = _strip_agent_namespace_suffix(agent_identifier)
        if name is None:
            return None

        # Special check for the router agent:
        # The router is a built-in agent that handles command routing and doesn't
        # appear in config.agents. Without this check, extract_agent_name() would
        # return None for router messages, causing other agents to incorrectly
        # respond to router's error messages (e.g., when schedule parsing fails).
        if name == ROUTER_AGENT_NAME or name in config.agents or name in config.teams:
            return name
        return None

    def __str__(self) -> str:
        """Return the full Matrix ID string representation."""
        return self.full_id


@dataclass(frozen=True)
class _ThreadStateKey:
    """Represents a thread state key like 'thread_id:agent_name'."""

    thread_id: str
    agent_name: str

    @classmethod
    def parse(cls, state_key: str) -> _ThreadStateKey:
        """Parse a state key."""
        parts = state_key.split(":", 1)
        if len(parts) != 2:
            msg = f"Invalid state key: {state_key}"
            raise ValueError(msg)
        return cls(thread_id=parts[0], agent_name=parts[1])

    @property
    def key(self) -> str:
        """Get the full state key."""
        return f"{self.thread_id}:{self.agent_name}"

    def __str__(self) -> str:
        """Return the state key string representation."""
        return self.key


@lru_cache(maxsize=512)
def _parse_matrix_id(matrix_id: str) -> MatrixID:
    """Cached wrapper around MatrixID.parse for performance."""
    if not matrix_id.startswith("@"):
        msg = f"Invalid Matrix ID: {matrix_id}"
        raise ValueError(msg)
    if ":" not in matrix_id:
        msg = f"Invalid Matrix ID, missing domain: {matrix_id}"
        raise ValueError(msg)
    parts = matrix_id[1:].split(":", 1)
    if len(parts) != 2:
        msg = f"Invalid Matrix ID format: {matrix_id}"
        raise ValueError(msg)

    return MatrixID(username=parts[0], domain=parts[1])


def is_agent_id(matrix_id: str, config: Config) -> bool:
    """Quick check if a Matrix ID is an agent."""
    return extract_agent_name(matrix_id, config) is not None


def extract_agent_name(sender_id: str, config: Config) -> str | None:
    """Extract agent name from Matrix user ID like @mindroom_calculator:localhost.

    Returns agent name (e.g., 'calculator') or None if not an agent.
    """
    if not sender_id.startswith("@") or ":" not in sender_id:
        return None
    mid = MatrixID.parse(sender_id)
    return mid.agent_name(config)


def agent_username_localpart(agent_name: str) -> str:
    """Build the Matrix username localpart for an agent-like entity."""
    namespace = mindroom_namespace()
    if namespace:
        return f"{MatrixID.AGENT_PREFIX}{agent_name}_{namespace}"
    return f"{MatrixID.AGENT_PREFIX}{agent_name}"


def room_alias_localpart(room_alias: str) -> str | None:
    """Extract the localpart from a room alias like '#lobby:example.com' → 'lobby'."""
    if not room_alias.startswith("#") or ":" not in room_alias:
        return None
    return room_alias[1:].split(":", 1)[0]


def extract_server_name_from_homeserver(homeserver: str) -> str:
    """Extract server name from a homeserver URL like "http://localhost:8008".

    If MATRIX_SERVER_NAME environment variable is set, use that instead.
    This is needed for federation setups where the internal hostname differs
    from the actual Matrix server name.
    """
    # Check for explicit server name override (for federation/docker setups)
    if MATRIX_SERVER_NAME:
        return MATRIX_SERVER_NAME

    # Otherwise extract from homeserver URL
    # Remove protocol
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver

    # Remove port if present
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part
