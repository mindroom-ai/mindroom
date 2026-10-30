"""Memory scopes: which scopes an agent reads and a turn writes, and the one store root of each team scope."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.resolution import memory_backend

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.memory import MemoryBackend
    from mindroom.constants import RuntimePaths

_TEAM_MEMORY_DIRNAME = "team_memory"
TEAM_SCOPE_PREFIX = "team_"
# Most filesystems allow a directory name of at most 255 bytes, and a scope directory name is ASCII.
_MAX_SCOPE_DIR_NAME = 255


@dataclass(frozen=True)
class MemoryScope:
    """One memory scope: its scoped user ID and the root of the one store that holds it."""

    user_id: str
    root: Path


def team_user_id(member_names: Sequence[str]) -> str:
    """Return the memory scope ID of the team made of ``member_names``, the same for every order."""
    return f"{TEAM_SCOPE_PREFIX}{'+'.join(sorted(member_names))}"


def _scope_dir_name(scope_user_id: str) -> str:
    """Return the directory name of one team scope: its ID, with a long one shortened and told apart by a hash.

    A configured scope ID is already a valid name; replacing other characters keeps a planted record's scope ID, which
    the startup merge reads, from reaching outside the team memory root.
    """
    name = re.sub(r"[^a-zA-Z0-9._+-]+", "_", scope_user_id)
    if len(name) <= _MAX_SCOPE_DIR_NAME:
        return name
    digest = hashlib.sha256(scope_user_id.encode()).hexdigest()[:16]
    return f"{name[: _MAX_SCOPE_DIR_NAME - len(digest) - 1]}-{digest}"


def team_scope(scope_user_id: str, runtime_paths: RuntimePaths) -> MemoryScope:
    """Return one team scope with its own store root, which no requester or member partitions."""
    return MemoryScope(
        scope_user_id,
        runtime_paths.storage_root / _TEAM_MEMORY_DIRNAME / _scope_dir_name(scope_user_id),
    )


def caller_memory_backend(config: Config, caller: str | Sequence[str]) -> MemoryBackend:
    """Return an agent's memory backend, or a team's: file when all members use file, none when all use none, else mem0."""
    if isinstance(caller, str):
        return memory_backend(config, caller)
    config.assert_team_agents_supported(list(caller))
    backends = {memory_backend(config, agent_name) for agent_name in caller}
    return backends.pop() if len(backends) == 1 else "mem0"


def team_scopes(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> list[MemoryScope]:
    """Return the scopes of the configured teams an agent belongs to, one per member set, which it reads too.

    A member reads every team's store through its own backend, so a team's older store stays readable after a
    teammate's backend change sends the team's new memories to another backend.
    """
    scope_user_ids: list[str] = []
    for team_name, team_config in config.teams.items():
        if agent_name not in team_config.agents:
            continue
        config.assert_team_agents_supported(team_config.agents, team_name=team_name)
        if (scope_user_id := team_user_id(team_config.agents)) not in scope_user_ids:
            scope_user_ids.append(scope_user_id)
    return [team_scope(scope_user_id, runtime_paths) for scope_user_id in scope_user_ids]
