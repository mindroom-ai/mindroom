"""Whether a person's tailnet SSH link to an agent's worker may exist: the one rule config, launch, and links share.

A link is eligible only on a dedicated worker backend, for an agent that opts in with ``tailnet_ssh: true``, whose
worker is per person and agent, whose workspace is that person's alone, and whose worker mounts no shared directory
another agent owns. ``worker_scope`` is a reuse rule, not a data partition, so the rule names the workspace's owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

from mindroom.access_policy import resolve_responder_access
from mindroom.config.resolution import agent_policy
from mindroom.matrix_identifiers import is_concrete_matrix_user_id
from mindroom.shared_directories.store import is_shareable_agent, load_shared_directories, shared_directory_access
from mindroom.tool_system.worker_routing import (
    normalize_worker_key_part,
    user_agent_worker_key_requester,
    worker_key_agent_name,
)
from mindroom.workers.backend_kinds import worker_backend_kind

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.shared_directories.store import SharedDirectory
    from mindroom.workers.models import WorkerLaunchConfig

_Reason = Literal["backend", "not_enabled", "scope", "shared_workspace", "joined_directory", "not_authorized"]


@dataclass(frozen=True)
class _Ineligible:
    """The first condition a link fails; unresolved membership is retryable on connect."""

    reason: _Reason
    pending: bool = False


class _SharedMount(Protocol):
    """A shared directory a worker mounts; the rule reads only whether it is read-only."""

    @property
    def read_only(self) -> bool: ...


def _concrete_users_alone(config: Config, agent_name: str) -> tuple[str, ...] | None:
    """Return the users an agent admits when only concrete users are admitted, with no room grants, else None."""
    access = resolve_responder_access(config, agent_name)
    if (
        not access.users
        or not all(is_concrete_matrix_user_id(user_id) for user_id in access.users)
        or access.members_of_rooms
        or access.current_room_members
    ):
        return None
    return access.users


def _key_agent(worker_key: str, agent_names: Iterable[str]) -> str | None:
    """Return the one agent an unpinned ``user_agent`` key's agent part encodes, or None when none or several do.

    Configured names such as ``mine`` and ``mine_`` normalize to the same part, so such a key is no one agent's.
    """
    agent_part = worker_key_agent_name(worker_key)
    if user_agent_worker_key_requester(worker_key) is None or agent_part is None:
        return None
    names = [name for name in agent_names if normalize_worker_key_part(name) == agent_part]
    return names[0] if len(names) == 1 else None


def _has_joined_directory(config: Config, agent_name: str, directories: Iterable[SharedDirectory]) -> bool:
    """Whether the agent mounts a shared directory it does not own."""
    shareable = partial(is_shareable_agent, config)
    return any(shared_directory_access(directory, (agent_name,), shareable) is False for directory in directories)


def config_error(config: Config, agent_name: str) -> str | None:
    """Reject opt-in that cannot work; the sole-user requirement is checked at launch and connect."""
    if not config.agents[agent_name].tailnet_ssh:
        return None
    policy = agent_policy(config, agent_name)
    prefix = f"agents.{agent_name}.tailnet_ssh requires "
    if normalize_worker_key_part(agent_name) != agent_name:
        return f"{prefix}an agent name that round-trips as a worker key part; rename this agent."
    if policy.effective_execution_scope != "user_agent":
        return f"{prefix}worker_scope: user_agent or private.per: user_agent, not {policy.scope_label}."
    if not policy.is_private and _concrete_users_alone(config, agent_name) is None:
        return (
            f"{prefix}a private agent, or access.users of concrete Matrix user IDs "
            "with members_of_rooms: [] and current_room_members off."
        )
    return None


def _not_authorized(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str,
    agent_name: str,
    worker_key: str,
    membership_index: AgentReplyMembershipIndex,
) -> _Ineligible | None:
    """Return ``not_authorized`` unless the key is the person's and they may use the agent, pending while not known."""
    # why-lazy: authorization loads nio; the worker backends import this module for the launch rule, and the
    # sandbox runner imports them.
    from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder  # noqa: PLC0415

    if user_agent_worker_key_requester(worker_key) != requester_id:
        return _Ineligible("not_authorized")
    try:
        allowed = is_sender_allowed_for_responder(
            requester_id,
            agent_name,
            None,
            config,
            runtime_paths,
            membership_index,
            require_resolved_membership=True,
        )
    except ReplyMembershipPendingError:
        return _Ineligible("not_authorized", pending=True)
    return None if allowed else _Ineligible("not_authorized")


def link_ineligibility(
    config: Config,
    runtime_paths: RuntimePaths,
    requester_id: str,
    agent_name: str,
    worker_key: str | None,
    membership_index: AgentReplyMembershipIndex,
    directories: Sequence[SharedDirectory] | None = None,
) -> _Ineligible | None:
    """Return the first failed link check, optionally reusing a shared-directory snapshot."""
    if not worker_backend_kind(runtime_paths).dedicated:
        return _Ineligible("backend")
    agent = config.agents.get(agent_name)
    if agent is None or not agent.tailnet_ssh:
        return _Ineligible("not_enabled")
    policy = agent_policy(config, agent_name)
    if (
        policy.effective_execution_scope != "user_agent"
        or normalize_worker_key_part(agent_name) != agent_name
        or worker_key is None
        or _key_agent(worker_key, config.agents) != agent_name
    ):
        return _Ineligible("scope")
    denied = _not_authorized(config, runtime_paths, requester_id, agent_name, worker_key, membership_index)
    if denied is not None:
        return denied
    if not policy.is_private and _concrete_users_alone(config, agent_name) != (requester_id,):
        return _Ineligible("shared_workspace")
    if directories is None:
        directories = load_shared_directories(runtime_paths)
    return _Ineligible("joined_directory") if _has_joined_directory(config, agent_name, directories) else None


def ssh_eligible_launch(
    worker_key: str,
    launch: WorkerLaunchConfig,
    private_agent_names: frozenset[str] | None,
    shared_mounts: Sequence[_SharedMount],
) -> bool:
    """Return whether a worker about to launch with ``shared_mounts`` gets the tailnet SSH mark.

    The person is the key's requester and the agent the one launch agent whose name the key's agent part encodes;
    a private agent must be one the worker may see. A read-only shared mount is a directory another agent owns.
    """
    agent_name = _key_agent(worker_key, launch.agent_policies)
    if agent_name is None or normalize_worker_key_part(agent_name) != agent_name:
        return False
    requester_id = user_agent_worker_key_requester(worker_key)
    policy = launch.agent_policies[agent_name]
    agent = launch.config.agents.get(agent_name)
    if agent is None or not agent.tailnet_ssh or policy.effective_execution_scope != "user_agent":
        return False
    if policy.is_private:
        if private_agent_names is None or agent_name not in private_agent_names:
            return False
    elif _concrete_users_alone(launch.config, agent_name) != (requester_id,):
        return False
    return not any(mount.read_only for mount in shared_mounts)
