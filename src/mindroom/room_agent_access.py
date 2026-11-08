"""Which of a room's agents a Matrix user may use there, and the workspace each of those agents gives them.

The worker Computer and plugin routes share one check: the agent's user ID names a current configured agent, it and
the requester are both joined to the room, and once the reply-membership index is current the agent's access rules
let the requester converse with it there. A refusal raises ``HTTPException``: 403 for a proven no, 503 while agent
identities or the membership policy are unresolved.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from mindroom.agent_reply_membership import agent_reply_membership_policy_changed
from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import contact_room_ids
from mindroom.entity_resolution import (
    MissingManagedEntityAccountError,
    current_entity_id,
    entity_identity_registry,
)
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.filters import tool_name_allowed
from mindroom.tool_system.metadata import normalize_string_array_override, resolve_tool_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping
    from pathlib import Path

    from fastapi import Request

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.plugin_http import RoomPower
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

# As long as plugin routes wait for a room read, and the Computer for its whole check.
_ACCESS_TIMEOUT_SECONDS = 20
_ACCESS_UNAVAILABLE = "Agent access is unavailable; try again."
# The functions that write any content to any path of the workspace or run code there, by toolkit; delete alone
# edits nothing. Any function of another toolkit that runs code counts.
_WRITERS = {
    "file": frozenset({"save_file", "replace_file_chunk"}),
    "coding": frozenset({"write_file", "edit_file"}),
    "python": frozenset(
        {
            "run_python_code",
            "save_to_file_and_run",
            "run_python_file_return_variable",
            "pip_install_package",
            "uv_pip_install_package",
        },
    ),
    "script": frozenset({"start_script"}),
    "claude_agent": frozenset({"claude_start_session", "claude_send"}),
    "docker": frozenset({"build_image", "run_container", "start_container", "exec_in_container"}),
}


def _may_converse(
    requester_id: str,
    agent_name: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> bool:
    try:
        return is_sender_allowed_for_responder(
            requester_id,
            agent_name,
            room_id,
            config,
            runtime_paths,
            memberships,
            require_resolved_membership=True,
        )
    except ReplyMembershipPendingError:
        raise HTTPException(503, "Responder membership policy is unresolved.") from None


@contextmanager
def _leaving_out(agent_name: str, room_id: str, *, leave_out: bool) -> Iterator[None]:
    """With ``leave_out``, leave an agent out of a listing, logging why, when resolving it fails; otherwise raise.

    These failures belong to one agent: its account is not prepared, its grant rooms are unresolved, or its contacts
    or stored tool settings are corrupt.
    """
    try:
        yield
    except (HTTPException, MissingManagedEntityAccountError, ValueError, TypeError) as error:
        if not leave_out:
            raise
        logger.warning(
            "room_agent_left_out",
            agent_name=agent_name,
            room_id=room_id,
            reason=f"{type(error).__name__}: {error}",
        )


async def _bring_memberships_current(
    requester_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[bool], Awaitable[None]],
) -> None:
    """Refresh ``memberships`` when they need it or lack ``requester_id``, whom the room's live roster showed joined.

    A refresh already in flight may have read the room before the requester joined, so grants that still lack them
    after it get one more.
    """
    await refresh_memberships(memberships.misses_member(requester_id, room_id, config, runtime_paths))
    if memberships.misses_member(requester_id, room_id, config, runtime_paths):
        await refresh_memberships(True)


async def admit_room_agent(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    joined_members: Callable[[], Awaitable[Collection[str]]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[bool], Awaitable[None]],
) -> str:
    """Return the name of the agent ``agent_user_id`` names once ``requester_id`` may use it in ``room_id``.

    ``joined_members`` returns the room's joined users; the check awaits it only once the agent is found.
    ``refresh_memberships`` refreshes ``memberships`` when they need it, or when its argument says they are stale, as
    they are while they lack a requester the room's live roster shows joined.
    """
    try:
        identities = entity_identity_registry(config, runtime_paths)
    except MissingManagedEntityAccountError:
        raise HTTPException(503, "Agent identities are not ready.") from None
    name = identities.current_entity_name_for_user_id(agent_user_id, include_router=False)
    if name is None or name not in config.agents:
        raise HTTPException(403, "The selected user is not a current agent.")
    joined = await joined_members()
    if requester_id not in joined or agent_user_id not in joined:
        raise HTTPException(403, "Requester and agent must both be joined to this room.")
    await _bring_memberships_current(requester_id, room_id, config, runtime_paths, memberships, refresh_memberships)
    if not _may_converse(requester_id, name, room_id, config, runtime_paths, memberships):
        raise HTTPException(403, "Requester cannot use this agent.")
    return name


async def _admitted_agents(
    requester_id: str,
    room_id: str,
    joined: Collection[str],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[bool], Awaitable[None]],
) -> list[tuple[str, str]]:
    """Return each agent joined to the room that ``requester_id`` may use there, by name and user ID, in config order.

    It leaves out an agent without its account or whose own grant rooms are unresolved, and answers 503 while the
    router's rooms are unknown and an agent's access waits on them.
    """
    joined_agents: list[tuple[str, str]] = []
    for name in config.agents:
        with _leaving_out(name, room_id, leave_out=True):
            user_id = current_entity_id(name, runtime_paths).full_id
            if user_id in joined:
                joined_agents.append((name, user_id))
    if not joined_agents:
        return []
    await _bring_memberships_current(requester_id, room_id, config, runtime_paths, memberships, refresh_memberships)
    # Without the router's rooms every room grant is unresolved, and an empty answer would read as no agents.
    rooms_known = memberships.knows_joined_rooms()
    admitted: list[tuple[str, str]] = []
    for name, user_id in joined_agents:
        with _leaving_out(name, room_id, leave_out=rooms_known):
            if _may_converse(requester_id, name, room_id, config, runtime_paths, memberships):
                admitted.append((name, user_id))
    return admitted


def room_execution_identity(
    agent_name: str,
    requester_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ToolExecutionIdentity:
    """Return the identity by which a turn of ``agent_name`` for ``requester_id`` in ``room_id`` resolves its worker.

    A turn resolves a bridge alias to the person it stands for, and its thread never changes its worker or workspace.
    """
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=resolve_human_requester_alias(requester_id, config, runtime_paths),
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )


@dataclass(frozen=True)
class AgentWorkspace:
    """One agent a caller may use in a room, and its workspace as a turn of it for that caller there resolves it."""

    name: str
    user_id: str
    display_name: str
    private: bool
    """Whether the agent is private, so each caller has a workspace of their own."""
    root: Path
    """The workspace folder, which may not exist before the agent's first turn for this caller."""
    files: Literal["none", "read", "write"]
    """``read`` with a toolkit that takes local paths or runs code, ``write`` with one that edits files or runs code
    and power to post messages in the room."""
    terminal: Literal["none", "watch", "type"]
    """``watch`` with a shell, ``type`` with a shell and power to post messages in the room."""
    config: Config = field(repr=False)
    """The config this answer was resolved under, the one the API served when the request arrived."""


def _offered_functions(metadata: ToolMetadata, settings: Mapping[str, object]) -> frozenset[str] | None:
    """Return the toolkit's functions its settings leave on: Agno's ``all`` or ``enable_<function>``, then filters.

    None when the toolkit declares no function names, as a plugin or MCP server may, and no allowlist names them.
    """
    values = {config_field.name: config_field.default for config_field in metadata.config_fields or ()} | dict(settings)
    include, exclude = (
        normalize_string_array_override(values.get(name), preserve_empty_list=True)
        for name in ("include_tools", "exclude_tools")
    )
    available = metadata.function_names or include
    if available is None:
        return None
    return frozenset(
        function
        for function in available
        if (values.get("all") or values.get(f"enable_{function}", True))
        and tool_name_allowed(function, include=include, exclude=exclude)
    )


def _workspace(
    name: str,
    user_id: str,
    requester_id: str,
    room_id: str,
    *,
    may_send: bool,
    config: Config,
    runtime_paths: RuntimePaths,
) -> AgentWorkspace:
    identity = room_execution_identity(name, requester_id, room_id, config, runtime_paths)
    storage = resolve_agent_storage(name, config, runtime_paths, identity)
    workspace = resolve_agent_workspace_from_state_path(
        name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=storage.state_root,
        use_state_storage_path=storage.execution.policy.private_workspace_enabled,
    )
    tools = config_plugins(config, runtime_paths).tools
    # Each toolkit's settings, as the agent's turn builds it, decide which of its functions it offers; by name, whether
    # one of them writes files or runs code.
    path_toolkits: dict[str, bool] = {}
    for entry in resolve_agent_tools(config, name, tools).candidates(matrix_room_tools=True):
        metadata = tools.get(entry.name)
        # Code a toolkit runs works in the workspace whatever paths the toolkit takes.
        if metadata is None or (metadata.file_access is ToolFileAccess.NONE and not metadata.executes_code):
            continue
        settings = resolve_tool_settings(
            entry.name,
            runtime_paths,
            tools=tools,
            worker_target=storage.execution.worker_target,
            allowed_shared_services=(
                config.get_worker_grantable_credentials() if storage.execution.execution_scope is not None else None
            ),
            tool_config_overrides=entry.tool_config_overrides,
        )
        # A toolkit whose functions only its build could tell offers what it builds.
        functions = _offered_functions(metadata, settings.values)
        if functions is not None and not functions:
            continue
        writers = _WRITERS.get(entry.name)
        path_toolkits[entry.name] = (
            metadata.executes_code if writers is None else not writers.isdisjoint(functions or ())
        )
    return AgentWorkspace(
        name=name,
        user_id=user_id,
        display_name=config.agents[name].display_name,
        private=storage.execution.is_private,
        root=workspace.root,
        files="none" if not path_toolkits else "write" if may_send and any(path_toolkits.values()) else "read",
        terminal="none" if "shell" not in path_toolkits else "type" if may_send else "watch",
        config=config,
    )


async def room_agent_workspaces(
    request: Request,
    user_id: str,
    room_id: str,
    members: Collection[str],
    power: Callable[[], Awaitable[RoomPower]],
    agent_user_id: str | None,
) -> list[AgentWorkspace]:
    """Return the workspaces ``user_id``, joined to the room with ``members``, may open there, under the served config.

    Given ``agent_user_id``, only that agent's, answering as the Computer does; otherwise every one in config order,
    leaving out, with a logged reason, one that cannot be resolved. A contact room opens none. Writing and typing need
    power to post messages in the room.
    """
    snapshot = bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    runtime = ensure_app_state(request.app).embedded
    router = runtime.agent_bot(ROUTER_AGENT_NAME) if runtime is not None else None
    if (
        config is None
        or runtime is None
        or router is None
        or router.client is None
        or not router.running
        # The runtime's grants follow its own config; a refresh under another membership policy would replace them.
        or agent_reply_membership_policy_changed(router.config, config)
    ):
        raise HTTPException(503, _ACCESS_UNAVAILABLE)
    runtime_paths = snapshot.runtime_paths
    listing = agent_user_id is None
    try:
        async with asyncio.timeout(_ACCESS_TIMEOUT_SECONDS):
            if agent_user_id is None:
                admitted = await _admitted_agents(
                    user_id,
                    room_id,
                    members,
                    config=config,
                    runtime_paths=runtime_paths,
                    memberships=runtime.agent_reply_memberships,
                    refresh_memberships=runtime.refresh_agent_reply_memberships_when_needed,
                )
            else:

                async def joined_members() -> Collection[str]:
                    return members

                name = await admit_room_agent(
                    user_id,
                    room_id,
                    agent_user_id,
                    joined_members,
                    config=config,
                    runtime_paths=runtime_paths,
                    memberships=runtime.agent_reply_memberships,
                    refresh_memberships=runtime.refresh_agent_reply_memberships_when_needed,
                )
                admitted = [(name, agent_user_id)]
    except TimeoutError:
        raise HTTPException(503, _ACCESS_UNAVAILABLE) from None
    opened: list[tuple[str, str]] = []
    for name, agent_id in admitted:
        with _leaving_out(name, room_id, leave_out=listing):
            # Every turn in a contact room runs as its agent's owner, whom the other side must never reach through it.
            if room_id not in contact_room_ids(config, runtime_paths, name, any_status=True):
                opened.append((name, agent_id))
    if not opened:
        if not listing:
            raise HTTPException(403, "No workspace opens through a contact room.")
        return []
    may_send = (await power()).may_send(user_id, "m.room.message")
    workspaces: list[AgentWorkspace] = []
    for name, agent_id in opened:
        with _leaving_out(name, room_id, leave_out=listing):
            workspaces.append(
                _workspace(
                    name,
                    agent_id,
                    user_id,
                    room_id,
                    may_send=may_send,
                    config=config,
                    runtime_paths=runtime_paths,
                ),
            )
    return workspaces
