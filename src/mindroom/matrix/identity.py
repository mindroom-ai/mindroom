"""Unified Matrix ID handling system."""

from dataclasses import dataclass
from functools import lru_cache
from typing import ClassVar

from ..agent_config import load_config
from ..constants import ROUTER_AGENT_NAME


@dataclass(frozen=True)
class MatrixID:
    """Immutable Matrix ID representation with parsing and validation."""

    username: str
    domain: str

    # Class constants
    AGENT_PREFIX: ClassVar[str] = "mindroom_"
    DEFAULT_DOMAIN: ClassVar[str] = "mindroom.space"

    @classmethod
    def parse(cls, matrix_id: str) -> "MatrixID":
        """Parse a Matrix ID like @mindroom_calculator:localhost."""
        if not matrix_id.startswith("@"):
            raise ValueError(f"Invalid Matrix ID: {matrix_id}")

        parts = matrix_id[1:].split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid Matrix ID format: {matrix_id}")

        return cls(username=parts[0], domain=parts[1])

    @classmethod
    def from_agent(cls, agent_name: str, domain: str) -> "MatrixID":
        """Create a MatrixID for an agent."""
        return cls(username=f"{cls.AGENT_PREFIX}{agent_name}", domain=domain)

    @classmethod
    def from_username(cls, username: str, domain: str) -> "MatrixID":
        """Create a MatrixID from a username (without @ prefix)."""
        return cls(username=username, domain=domain)

    @property
    def full_id(self) -> str:
        """Get the full Matrix ID like @mindroom_calculator:localhost."""
        return f"@{self.username}:{self.domain}"

    @property
    def is_agent(self) -> bool:
        """Check if this is an agent ID."""
        return self.username.startswith(self.AGENT_PREFIX)

    @property
    def is_mindroom_domain(self) -> bool:
        """Check if this is on the mindroom.space domain."""
        return self.domain == self.DEFAULT_DOMAIN

    @property
    def agent_name(self) -> str | None:
        """Extract agent name if this is an agent ID."""
        if not self.is_agent:
            return None

        # Remove prefix
        name = self.username[len(self.AGENT_PREFIX) :]

        # Special check for the router agent:
        # The router is a built-in agent that handles command routing and doesn't
        # appear in config.agents. Without this check, extract_agent_name() would
        # return None for router messages, causing other agents to incorrectly
        # respond to router's error messages (e.g., when schedule parsing fails).
        if name == ROUTER_AGENT_NAME:
            return name

        # Validate regular agents against config
        config = load_config()
        return name if name in config.agents else None

    def __str__(self) -> str:
        return self.full_id


@dataclass(frozen=True)
class ThreadStateKey:
    """Represents a thread state key like 'thread_id:agent_name'."""

    thread_id: str
    agent_name: str

    @classmethod
    def parse(cls, state_key: str) -> "ThreadStateKey":
        """Parse a state key."""
        parts = state_key.split(":", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid state key: {state_key}")
        return cls(thread_id=parts[0], agent_name=parts[1])

    @property
    def key(self) -> str:
        """Get the full state key."""
        return f"{self.thread_id}:{self.agent_name}"

    def __str__(self) -> str:
        return self.key


@lru_cache(maxsize=256)
def parse_matrix_id(matrix_id: str) -> MatrixID:
    """Cached parsing of Matrix IDs."""
    return MatrixID.parse(matrix_id)


def is_agent_id(matrix_id: str) -> bool:
    """Quick check if a Matrix ID is an agent."""
    if not matrix_id.startswith("@") or ":" not in matrix_id:
        return False
    mid = parse_matrix_id(matrix_id)
    return mid.is_agent and mid.agent_name is not None


def extract_agent_name(sender_id: str) -> str | None:
    """Extract agent name from Matrix user ID like @mindroom_calculator:localhost.

    Returns agent name (e.g., 'calculator') or None if not an agent.
    """
    if not sender_id.startswith("@") or ":" not in sender_id:
        return None
    mid = parse_matrix_id(sender_id)
    return mid.agent_name


def extract_server_name_from_homeserver(homeserver: str) -> str:
    """Extract server name from a homeserver URL like "http://localhost:8008"."""
    # Remove protocol
    server_part = homeserver.split("://", 1)[1] if "://" in homeserver else homeserver

    # Remove port if present
    if ":" in server_part:
        return server_part.split(":", 1)[0]
    return server_part
