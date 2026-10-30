"""Memory scopes: which scopes an agent reads and a turn writes, and where team scopes are stored."""

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
    """One memory scope: its scoped user ID and the root of the store that holds it."""

    user_id: str
    root: Path


def team_user_id(member_names: Sequence[str]) -> str:
    """Return the memory scope ID of the team made of ``member_names``, the same for every order."""
    return f"{TEAM_SCOPE_PREFIX}{'+'.join(sorted(member_names))}"


def _scope_dir_name(scope_user_id: str) -> str:
    """Return the directory name of one team scope, with a long one shortened and told apart by a hash.

    Replacing characters outside the allowed set keeps a planted record's scope ID, which the startup merge reads, from
    reaching outside the team memory root.
    """
    # LEGACY_COMPAT: Team file copies named after their scope ID without a trailing underscore.
    # Legacy format: each member's `agents/<member>/memory_files/<dir>/` team copy, `<dir>` being the scope ID with
    #   characters outside `[a-zA-Z0-9._+-]` replaced by `_` and leading and trailing underscores dropped.
    # Last legacy release: v2026.10.711; replacement: unreleased; the team's one directory below `team_memory/` keeps
    #   that name.
    # Handling: team directories keep the copies' name, shortened with a hash only past 255 bytes, so the startup merge
    #   moves each copy to the directory its team reads; scopes differing only in a trailing underscore share one
    #   directory, as their copies did.
    # Coverage:
    #   tests/test_legacy_team_memory.py::test_file_copies_of_members_with_edge_underscores_merge_where_the_team_reads,
    #   tests/test_legacy_team_memory.py::test_two_scopes_sharing_a_directory_each_keep_a_fact_they_share,
    #   tests/test_memory_policy.py::test_team_scope_directories_keep_earlier_names_within_the_name_limit.
    name = re.sub(r"[^a-zA-Z0-9._+-]+", "_", scope_user_id).strip("_")
    if len(name) <= _MAX_SCOPE_DIR_NAME:
        return name
    digest = hashlib.sha256(scope_user_id.encode()).hexdigest()[:16]
    return f"{name[: _MAX_SCOPE_DIR_NAME - len(digest) - 1]}-{digest}"


def team_memory_root(runtime_paths: RuntimePaths) -> Path:
    """Return the team memory root: the one Mem0 store every team scope shares, beside each team's file directory."""
    return runtime_paths.storage_root / _TEAM_MEMORY_DIRNAME


def team_scope(scope_user_id: str, runtime_paths: RuntimePaths) -> MemoryScope:
    """Return one team scope with its own file-memory directory, which no requester or member partitions."""
    return MemoryScope(scope_user_id, team_memory_root(runtime_paths) / _scope_dir_name(scope_user_id))


def caller_memory_backend(config: Config, caller: str | Sequence[str]) -> MemoryBackend:
    """Return an agent's memory backend, or a team's: file when all members use file, none when all use none, else mem0."""
    if isinstance(caller, str):
        return memory_backend(config, caller)
    config.assert_team_agents_supported(list(caller))
    backends = {memory_backend(config, agent_name) for agent_name in caller}
    return backends.pop() if len(backends) == 1 else "mem0"


def team_user_ids(agent_name: str, config: Config) -> list[str]:
    """Return the scope IDs of the configured teams an agent belongs to, one per member set, which it reads too.

    A member reads every team's memory through its own backend, so a team's older memories stay readable after a
    teammate's backend change sends the team's new memories to another backend.
    """
    scope_user_ids: dict[str, None] = {}
    for team_name, team_config in config.teams.items():
        if agent_name in team_config.agents:
            config.assert_team_agents_supported(team_config.agents, team_name=team_name)
            scope_user_ids[team_user_id(team_config.agents)] = None
    return list(scope_user_ids)


def team_scopes(agent_name: str, config: Config, runtime_paths: RuntimePaths) -> list[MemoryScope]:
    """Return the team scopes an agent reads, each with its file-memory directory."""
    return [team_scope(scope_user_id, runtime_paths) for scope_user_id in team_user_ids(agent_name, config)]
