"""Which of a room's agents a Matrix user may use there, and the workspace each of those agents gives them.

The worker Computer and plugin routes share one check: the agent's user ID names a current configured agent, it and
the requester are both joined to the room, and after a refresh of the reply-membership index the agent's access rules
let the requester converse with it there. A refusal raises ``HTTPException``: 403 for a proven no, 503 while agent
identities or the membership policy are unresolved.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException

from mindroom.agent_reply_membership import agent_reply_membership_policy_changed
from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import bind_current_request_snapshot
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import contact_room_ids
from mindroom.entity_resolution import (
    EntityIdentityRegistry,
    MissingManagedEntityAccountError,
    entity_identity_registry,
)
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
    from collections.abc import Awaitable, Callable, Collection, Mapping
    from pathlib import Path

    import nio
    from fastapi import Request

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.plugin_http import RoomPower
    from mindroom.tool_system.declarations import ToolMetadata

# As long as plugin routes wait for a room read, and the Computer for its whole check.
_ACCESS_TIMEOUT_SECONDS = 20
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


def _identities(config: Config, runtime_paths: RuntimePaths) -> EntityIdentityRegistry:
    try:
        return entity_identity_registry(config, runtime_paths)
    except MissingManagedEntityAccountError:
        raise HTTPException(503, "Agent identities are not ready.") from None


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


async def admit_room_agent(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    joined_members: Callable[[], Awaitable[Collection[str]]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    client: nio.AsyncClient,
    memberships: AgentReplyMembershipIndex,
) -> str:
    """Return the name of the agent ``agent_user_id`` names once ``requester_id`` may use it in ``room_id``.

    ``joined_members`` returns the room's joined users; the check awaits it only once the agent is found.
    """
    name = _identities(config, runtime_paths).current_entity_name_for_user_id(agent_user_id, include_router=False)
    if name is None or name not in config.agents:
        raise HTTPException(403, "The selected user is not a current agent.")
    joined = await joined_members()
    if requester_id not in joined or agent_user_id not in joined:
        raise HTTPException(403, "Requester and agent must both be joined to this room.")
    await memberships.refresh(config, runtime_paths, client)
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
    client: nio.AsyncClient,
    memberships: AgentReplyMembershipIndex,
) -> list[tuple[str, str]]:
    """Return each agent joined to the room that ``requester_id`` may use there, by name and user ID, in config order."""
    ids = _identities(config, runtime_paths).current_ids
    joined_agents = [(name, ids[name].full_id) for name in config.agents if ids[name].full_id in joined]
    if not joined_agents:
        return []
    await memberships.refresh(config, runtime_paths, client)
    return [
        (name, user_id)
        for name, user_id in joined_agents
        if _may_converse(requester_id, name, room_id, config, runtime_paths, memberships)
    ]


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
    """``read`` with a toolkit that takes local paths or runs code, ``write`` with one that edits files or runs code."""
    terminal: Literal["none", "watch", "type"]
    """``watch`` with a shell, ``type`` with a shell and power to post messages in the room."""
    config: Config = field(repr=False)
    """The config this answer was resolved under, the one the API served when the request arrived."""


def _offered_functions(metadata: ToolMetadata, settings: Mapping[str, object]) -> frozenset[str]:
    """Return the toolkit's functions its settings leave on: Agno's ``all`` or ``enable_<function>``, then filters."""
    values = {config_field.name: config_field.default for config_field in metadata.config_fields or ()} | dict(settings)
    include, exclude = (
        normalize_string_array_override(values.get(name), preserve_empty_list=True)
        for name in ("include_tools", "exclude_tools")
    )
    return frozenset(
        function
        for function in metadata.function_names
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
    # A turn's thread never changes which workspace it gets.
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    storage = resolve_agent_storage(name, config, runtime_paths, identity)
    workspace = resolve_agent_workspace_from_state_path(
        name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=storage.state_root,
        use_state_storage_path=storage.execution.policy.private_workspace_enabled,
    )
    tools = config_plugins(config, runtime_paths).tools
    # Each toolkit's settings, as the agent's turn builds it, decide which of its functions it offers.
    path_toolkits: dict[str, tuple[ToolMetadata, frozenset[str]]] = {}
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
        # A toolkit that declares no function names, as a plugin or MCP server may, offers what it builds.
        if (functions := _offered_functions(metadata, settings.values)) or not metadata.function_names:
            path_toolkits[entry.name] = (metadata, functions)
    writes = any(
        not functions.isdisjoint(_WRITERS[toolkit]) if toolkit in _WRITERS else metadata.executes_code
        for toolkit, (metadata, functions) in path_toolkits.items()
    )
    return AgentWorkspace(
        name=name,
        user_id=user_id,
        display_name=config.agents[name].display_name,
        private=storage.execution.is_private,
        root=workspace.root,
        files="none" if not path_toolkits else "write" if may_send and writes else "read",
        terminal="none" if "shell" not in path_toolkits else "type" if may_send else "watch",
        config=config,
    )


async def _known(members: Collection[str]) -> Collection[str]:
    """Return the members the lookup that proved the caller's membership read."""
    return members


async def room_agent_workspaces(
    request: Request,
    user_id: str,
    room_id: str,
    members: Collection[str],
    power: Callable[[], Awaitable[RoomPower]],
    agent_user_id: str | None,
) -> list[AgentWorkspace]:
    """Return the workspaces ``user_id``, joined to the room with ``members``, may open there, under the served config.

    Given ``agent_user_id``, only that agent's, answering as the Computer does; otherwise every one in config order.
    A contact room opens none. Writing and typing need power to post messages in the room.
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
        raise HTTPException(503, "Agent access is unavailable; try again.")
    runtime_paths = snapshot.runtime_paths
    try:
        async with asyncio.timeout(_ACCESS_TIMEOUT_SECONDS):
            if agent_user_id is None:
                admitted = await _admitted_agents(
                    user_id,
                    room_id,
                    members,
                    config=config,
                    runtime_paths=runtime_paths,
                    client=router.client,
                    memberships=runtime.agent_reply_memberships,
                )
            else:
                name = await admit_room_agent(
                    user_id,
                    room_id,
                    agent_user_id,
                    partial(_known, members),
                    config=config,
                    runtime_paths=runtime_paths,
                    client=router.client,
                    memberships=runtime.agent_reply_memberships,
                )
                admitted = [(name, agent_user_id)]
    except TimeoutError:
        raise HTTPException(503, "Agent access is unavailable; try again.") from None
    # Every turn in a contact room runs as its agent's owner, whom the other side must never reach through it.
    opened = [
        (name, agent_id)
        for name, agent_id in admitted
        if room_id not in contact_room_ids(config, runtime_paths, name, any_status=True)
    ]
    if agent_user_id is not None and not opened:
        raise HTTPException(403, "No workspace opens through a contact room.")
    if not opened:
        return []
    may_send = (await power()).may_send(user_id, "m.room.message")
    requester_id = resolve_human_requester_alias(user_id, config, runtime_paths)
    return [
        _workspace(
            name,
            agent_id,
            requester_id,
            room_id,
            may_send=may_send,
            config=config,
            runtime_paths=runtime_paths,
        )
        for name, agent_id in opened
    ]
