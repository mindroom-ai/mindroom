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
from mindroom.entity_resolution import MissingManagedEntityAccountError, current_entity_id
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.filters import tool_name_allowed
from mindroom.tool_system.metadata import normalize_string_array_override, resolve_tool_settings
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.workers.backend_kinds import worker_backend_kind
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator, Mapping
    from pathlib import Path

    from fastapi.requests import HTTPConnection

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.plugin_http import RoomPower
    from mindroom.tool_system.agent_tools import AgentTools
    from mindroom.tool_system.declarations import ToolMetadata
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

logger = get_logger(__name__)

# Set on the primary when its shared ``static_runner`` mounts the agents' storage, as the Kubernetes chart's does, so a
# toolkit routed there works in the agent's workspace; a runner that sees only its own scratch storage leaves it off.
_STATIC_RUNNER_AGENT_STORAGE_ENV = "MINDROOM_STATIC_RUNNER_AGENT_STORAGE"
# As long as plugin routes wait for a room read, and the Computer for its whole check.
_ACCESS_TIMEOUT_SECONDS = 20
_ACCESS_UNAVAILABLE = "Agent access is unavailable; try again."


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


def _agent_user_ids(room_id: str, config: Config, runtime_paths: RuntimePaths) -> dict[str, str]:
    """Return each configured agent's user ID by name, in config order, leaving out one without its account."""
    user_ids: dict[str, str] = {}
    for name in config.agents:
        with _leaving_out(name, room_id, leave_out=True):
            user_ids[name] = current_entity_id(name, runtime_paths).full_id
    return user_ids


async def _bring_memberships_current(
    requester_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[], Awaitable[None]],
) -> None:
    """Refresh ``memberships`` when they need it, and read the room again while they lack ``requester_id``.

    The room's live roster just showed the requester joined.
    """
    await memberships.refresh_reading_room(
        room_id,
        refresh_memberships,
        lambda: not memberships.misses_member(requester_id, room_id, config, runtime_paths),
    )


async def admit_room_agent(
    requester_id: str,
    room_id: str,
    agent_user_id: str,
    joined_members: Callable[[], Awaitable[Collection[str]]],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
    refresh_memberships: Callable[[], Awaitable[None]],
) -> str:
    """Return the name of the agent ``agent_user_id`` names once ``requester_id`` may use it in ``room_id``.

    ``joined_members`` returns the room's joined users; the check awaits it only once the agent is found.
    ``refresh_memberships`` refreshes ``memberships`` through the runtime's one refresh when they need it.
    """
    agent_ids = _agent_user_ids(room_id, config, runtime_paths)
    name = next((name for name, user_id in agent_ids.items() if user_id == agent_user_id), None)
    if name is None:
        # An agent without its account may be the one ``agent_user_id`` names.
        if len(agent_ids) < len(config.agents):
            raise HTTPException(503, "Agent identities are not ready.")
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
    refresh_memberships: Callable[[], Awaitable[None]],
) -> list[tuple[str, str]]:
    """Return each agent joined to the room that ``requester_id`` may use there, by name and user ID, in config order.

    It leaves out an agent without its account or whose own grant rooms are unresolved, and answers 503 while the
    router's rooms are unknown and an agent's access waits on them.
    """
    joined_agents = [
        (name, user_id)
        for name, user_id in _agent_user_ids(room_id, config, runtime_paths).items()
        if user_id in joined
    ]
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
    where it works in the workspace, not a ``static_runner`` that sees only its own storage, and power to post
    messages in the room."""
    terminal: Literal["none", "watch", "type"]
    """``watch`` with a shell that offers ``run_shell_command`` and runs in a worker, ``type`` where that worker mounts
    the workspace, as a dedicated worker does, and the caller may post messages in the room; never more than the
    agent's ``terminals`` setting allows."""
    worker_target: ResolvedWorkerTarget = field(repr=False)
    """The worker the agent's turns for this caller use, which ``mindroom.plugin_http.worker_relay`` reaches."""
    config: Config = field(repr=False)
    """The config this answer was resolved under, the one the API served when the request arrived."""


def _toolkit_host(
    agent_tools: AgentTools,
    tool_name: str,
    runtime_paths: RuntimePaths,
) -> Literal["primary", "workspace_worker", "scratch_worker"]:
    """Return where the agent's calls of ``tool_name`` run, as its turns place them.

    ``primary`` is the primary process; ``workspace_worker`` a worker that mounts the agent's workspace: a dedicated
    Docker or Kubernetes worker, or the shared ``static_runner`` where ``_STATIC_RUNNER_AGENT_STORAGE_ENV`` says it
    mounts the agents' storage; ``scratch_worker`` the shared runner otherwise, which sees only its own storage.
    """
    if not agent_tools.runs_in_worker(tool_name, tool_placement(runtime_paths, agent_tools.tools)):
        return "primary"
    if worker_backend_kind(runtime_paths).dedicated or runtime_paths.env_flag(_STATIC_RUNNER_AGENT_STORAGE_ENV):
        return "workspace_worker"
    return "scratch_worker"


def _offered_functions(
    metadata: ToolMetadata,
    settings: Mapping[str, object],
) -> tuple[frozenset[str] | None, bool]:
    """Return the toolkit's functions its settings leave on, and whether one it names as writing is among them.

    A function stays on by Agno's ``all`` or ``enable_<function>``, then the filters. The functions are None when the
    toolkit declares no function names, as a plugin or MCP server may, and no allowlist names them. A toolkit that
    names no writing functions writes when it runs code.
    """
    values = {config_field.name: config_field.default for config_field in metadata.config_fields or ()} | dict(settings)
    include, exclude = (
        normalize_string_array_override(values.get(name), preserve_empty_list=True)
        for name in ("include_tools", "exclude_tools")
    )

    def offered(names: Iterable[str]) -> frozenset[str]:
        return frozenset(
            function
            for function in names
            if (values.get("all") or values.get(f"enable_{function}", True))
            and tool_name_allowed(function, include=include, exclude=exclude)
        )

    available = metadata.function_names or include
    functions = None if available is None else offered(available)
    if not metadata.workspace_writers:
        return functions, metadata.executes_code
    return functions, bool(offered(metadata.workspace_writers))


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
    agent_tools = resolve_agent_tools(config, name, tools)
    # Each toolkit's settings, as the agent's turn builds it, decide which of its functions it offers, and its
    # declarations whether one of them writes files or runs code.
    path_toolkits: dict[str, bool] = {}
    for entry in agent_tools.candidates(matrix_room_tools=True):
        # It acts only through the shell, whose own entry counts what that shell may change.
        if entry.name == "terminal":
            continue
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
        functions, writes = _offered_functions(metadata, settings.values)
        if functions is not None and not functions:
            continue
        # On a runner that sees only its own storage, a toolkit writes there, not in the workspace.
        path_toolkits[entry.name] = writes and _toolkit_host(agent_tools, entry.name, runtime_paths) != "scratch_worker"
    # A terminal runs only beside a worker's shell commands (its only function); people type in it where that worker
    # sees the workspace, and get it only as far as the agent's config allows.
    shell = _toolkit_host(agent_tools, "shell", runtime_paths) if "shell" in path_toolkits else None
    terminal = config.agents[name].terminals
    if shell in (None, "primary"):
        terminal = "none"
    elif terminal == "type" and not (may_send and shell == "workspace_worker"):
        terminal = "watch"
    return AgentWorkspace(
        name=name,
        user_id=user_id,
        display_name=config.agents[name].display_name,
        private=storage.execution.is_private,
        root=workspace.root,
        files="none" if not path_toolkits else "write" if may_send and any(path_toolkits.values()) else "read",
        terminal=terminal,
        worker_target=storage.execution.worker_target,
        config=config,
    )


async def room_agent_workspaces(
    connection: HTTPConnection,
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
    snapshot = bind_current_request_snapshot(connection)
    config = snapshot.runtime_config
    runtime = ensure_app_state(connection.app).embedded
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
