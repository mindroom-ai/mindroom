"""Unified Matrix ID handling system."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, ClassVar

from mindroom import matrix_naming
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.matrix.state import managed_account_usernames

if TYPE_CHECKING:
    from mindroom.config.main import Config

__all__ = [
    "MatrixID",
    "active_internal_sender_ids",
    "extract_agent_name",
    "is_agent_id",
]


@dataclass(frozen=True)
class MatrixID:
    """Immutable Matrix ID representation with parsing and validation."""

    username: str
    domain: str

    AGENT_PREFIX: ClassVar[str] = matrix_naming.AGENT_USERNAME_PREFIX

    @classmethod
    def parse(cls, matrix_id: str) -> MatrixID:
        """Parse a Matrix ID like @mindroom_calculator:localhost."""
        return _parse_matrix_id(matrix_id)

    @classmethod
    def from_agent(
        cls,
        agent_name: str,
        domain: str,
        runtime_paths: RuntimePaths,
    ) -> MatrixID:
        """Create a MatrixID for an agent."""
        return cls(username=matrix_naming.agent_username_localpart(agent_name, runtime_paths), domain=domain)

    @classmethod
    def from_username(cls, username: str, domain: str) -> MatrixID:
        """Create a MatrixID from a username (without @ prefix)."""
        return cls(username=username, domain=domain)

    @property
    def full_id(self) -> str:
        """Get the full Matrix ID like @mindroom_calculator:localhost."""
        return f"@{self.username}:{self.domain}"

    def agent_name(self, config: Config, runtime_paths: RuntimePaths) -> str | None:
        """Extract agent name if this is one current live managed agent-like ID.

        Live internal sender trust only applies on the current runtime domain.
        Persisted current managed usernames are recognised there, even if they drift.
        """
        if self.domain != config.get_domain(runtime_paths) or not self.username.startswith(self.AGENT_PREFIX):
            return None

        persisted_usernames = managed_account_usernames(runtime_paths)
        for account_key, active_name in _active_managed_agent_account_names(config).items():
            expected_username = persisted_usernames.get(account_key) or matrix_naming.agent_username_localpart(
                active_name,
                runtime_paths,
            )
            if expected_username == self.username:
                return active_name
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


def is_agent_id(matrix_id: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Quick check if a Matrix ID is an agent."""
    return extract_agent_name(matrix_id, config, runtime_paths) is not None


def extract_agent_name(sender_id: str, config: Config, runtime_paths: RuntimePaths) -> str | None:
    """Extract agent name from Matrix user ID like @mindroom_calculator:localhost.

    Returns agent name (e.g., 'calculator') or None if not an agent.
    """
    if not sender_id.startswith("@") or ":" not in sender_id:
        return None
    mid = MatrixID.parse(sender_id)
    return mid.agent_name(config, runtime_paths)


def _active_managed_account_keys(config: Config) -> frozenset[str]:
    """Return persisted Matrix account keys for currently active managed accounts."""
    account_keys = {f"agent_{ROUTER_AGENT_NAME}"}
    account_keys.update(f"agent_{agent_name}" for agent_name in config.agents)
    account_keys.update(f"agent_{team_name}" for team_name in config.teams)
    if config.mindroom_user is not None:
        account_keys.add("agent_user")
    return frozenset(account_keys)


def _active_managed_agent_account_names(config: Config) -> dict[str, str]:
    """Return active persisted Matrix account keys mapped to agent/team/router names."""
    account_names = {f"agent_{ROUTER_AGENT_NAME}": ROUTER_AGENT_NAME}
    account_names.update({f"agent_{agent_name}": agent_name for agent_name in config.agents})
    account_names.update({f"agent_{team_name}": team_name for team_name in config.teams})
    return account_names


def _configured_active_account_sender_ids(config: Config, runtime_paths: RuntimePaths) -> dict[str, str]:
    """Return configured sender IDs for active managed accounts.

    These are only used as bootstrap fallbacks before a persisted username exists.
    """
    current_domain = config.get_domain(runtime_paths)
    sender_ids = {
        account_key: MatrixID.from_agent(agent_name, current_domain, runtime_paths).full_id
        for account_key, agent_name in _active_managed_agent_account_names(config).items()
    }
    mindroom_user_id = config.get_mindroom_user_id(runtime_paths)
    if mindroom_user_id is not None:
        sender_ids["agent_user"] = mindroom_user_id
    return sender_ids


def active_internal_sender_ids(config: Config, runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return sender IDs trusted for live authorization and relay decisions."""
    sender_ids: set[str] = set()
    current_domain = config.get_domain(runtime_paths)
    persisted_usernames = managed_account_usernames(runtime_paths)
    configured_sender_ids = _configured_active_account_sender_ids(config, runtime_paths)
    for account_key in _active_managed_account_keys(config):
        username = persisted_usernames.get(account_key)
        if username is None:
            sender_ids.add(configured_sender_ids[account_key])
            continue
        sender_ids.add(MatrixID.from_username(username, current_domain).full_id)
    return frozenset(sender_ids)
