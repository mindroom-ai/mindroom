"""Tools for sharing a directory with other agents."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.shared_directories.paths import agent_reads_shares_in_worker, agent_shared_directory_views
from mindroom.shared_directories.store import (
    PendingInvitation,
    SharedDirectoryError,
    accept_shared_directory_invitation,
    add_shared_directory_member,
    create_shared_directory,
    decline_shared_directory_invitation,
    delete_shared_directory,
    invite_pending_shared_directory_member,
    is_shareable_agent,
    pending_shared_directory_invitations,
    remove_shared_directory_member,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_WRITE_ACTIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "create": ((), ("agent_name", "owner")),
    "invite": (("agent_name",), ("owner",)),
    "accept": (("owner",), ("agent_name",)),
    "decline": (("owner",), ("agent_name",)),
    "remove": (("agent_name",), ("owner",)),
    "delete": ((), ("agent_name", "owner")),
}


def _payload(status: str, **fields: object) -> str:
    return custom_tool_payload("shared_directories", status, **fields)


def _pending_entries(invitations: Iterable[PendingInvitation]) -> list[dict[str, str]]:
    return [
        {"agent": invitation.agent, "requested_by": invitation.requester, "requested_at": invitation.requested_at}
        for invitation in invitations
    ]


def _requester_may_use(context: ToolRuntimeContext, agent_name: str) -> bool:
    """Return whether the requester may use an agent outside the current room."""
    return is_sender_allowed_for_responder(
        context.requester_id,
        agent_name,
        None,
        context.config,
        context.runtime_paths,
        context.require_agent_reply_memberships(),
    )


def _changed_message(context: ToolRuntimeContext, agent_names: Iterable[str], done: str) -> str:
    # Only dedicated workers mount shared directories one by one, so only agents using them restart on a change.
    names = [
        agent_name
        for agent_name in agent_names
        if agent_reads_shares_in_worker(context.config, context.runtime_paths, agent_name)
    ]
    if not names:
        return done
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{done} {joined}: worker restarts on the next tool call, ending running background shells."


def _invite_pending(context: ToolRuntimeContext, agent: str, name: str, agent_name: str) -> str:
    invitation = PendingInvitation(
        agent=agent_name,
        requester=context.requester_id,
        requested_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        before, after = invite_pending_shared_directory_member(
            context.runtime_paths,
            owner=agent,
            name=name,
            invitation=invitation,
        )
    except SharedDirectoryError as exc:
        return _payload("error", name=name, agent=agent_name, message=str(exc))
    if agent_name in before.members:
        message = f"{agent_name} is already a member."
    elif before.pending_for(agent_name) is not None:
        message = f"The invitation to {agent_name} is already waiting to be accepted."
    else:
        message = (
            f"The user you act for has no access to {agent_name} outside the current room, so the invitation "
            f"waits until a user who may use {agent_name} accepts it through {agent_name}."
        )
    return _payload(
        "pending",
        name=name,
        agent=agent_name,
        members=list(after.members),
        pending=_pending_entries(after.pending),
        message=message,
    )


def _answer_error(context: ToolRuntimeContext, agent: str, action: str) -> str | None:
    if not is_access_checked_requester_id(context.requester_id, context.config, context.runtime_paths):
        return f"Invitations can only be {action} on behalf of a user."
    if not _requester_may_use(context, agent):
        return (
            f"The user you act for has no access to {agent} outside the current room, so it cannot be {action}; "
            "access through access.current_room_members does not count."
        )
    return None


def _validate_write_params(action: str, name: str, agent_name: str | None, owner: str | None) -> str | None:
    spec = _WRITE_ACTIONS.get(action)
    if spec is None:
        msg = f"Unsupported action {action!r}. Expected one of: {', '.join(_WRITE_ACTIONS)}."
        return _payload("error", action=action, name=name, message=msg)
    required, forbidden = spec
    params = {"agent_name": agent_name, "owner": owner}
    if unexpected := [field for field in forbidden if params[field] is not None]:
        msg = f"Parameter(s) {', '.join(unexpected)} cannot be used with action {action!r}."
        return _payload("error", action=action, name=name, message=msg)
    if missing := [field for field in required if not (value := params[field]) or not value.strip()]:
        msg = f"Parameter(s) {', '.join(missing)} required for action {action!r}."
        return _payload("error", action=action, name=name, message=msg)
    return None


class SharedDirectoryTools(Toolkit):
    """Create directories other agents can read from their own workers."""

    offer_background_after = False
    offer_output_path = False

    def __init__(self, agent_name: str | None = None) -> None:
        # The calling agent, bound when the tool is built, so a team member shares as itself rather than as the team.
        self._agent_name = agent_name
        super().__init__(name="shared_directories", tools=[self.list_shared_directories, self.shared_directories_write])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Keep wait budgets on shared directory writes while omitting them from listing."""
        if function.name == "shared_directories_write":
            return replace(function, offer_background_after=True)
        return function

    def _context(self) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None or self._agent_name is None:
            return _payload("error", message="Shared directory tool context is unavailable in this runtime path.")
        if not is_shareable_agent(context.config, self._agent_name):
            return _payload("error", message="Private agents cannot share directories.")
        return context, self._agent_name

    @staticmethod
    def _invitation_error(context: ToolRuntimeContext, agent: str, agent_name: str) -> str | None:
        config = context.config
        if agent_name not in config.agents:
            return f"'{agent_name}' is not an agent."
        if agent_name == agent:
            return "You cannot invite yourself."
        if not is_shareable_agent(config, agent_name):
            return f"{agent_name} is private and cannot join shared directories."
        if not is_access_checked_requester_id(context.requester_id, config, context.runtime_paths):
            return "Agents can only be invited on behalf of a user."
        return None

    def _create(self, context: ToolRuntimeContext, agent: str, name: str) -> str:
        try:
            directory = create_shared_directory(context.runtime_paths, owner=agent, name=name)
        except SharedDirectoryError as exc:
            return _payload("error", name=name, message=str(exc))
        path = next(
            str(view.path)
            for view in agent_shared_directory_views(context.config, context.runtime_paths, agent)
            if view.directory == directory
        )
        return _payload("ok", name=name, path=path, message=_changed_message(context, [agent], "Created."))

    def _invite(self, context: ToolRuntimeContext, agent: str, name: str, agent_name: str) -> str:
        if (error := self._invitation_error(context, agent, agent_name)) is not None:
            return _payload("error", name=name, agent=agent_name, message=error)
        if not _requester_may_use(context, agent_name):
            return _invite_pending(context, agent, name, agent_name)
        try:
            directory, added = add_shared_directory_member(
                context.runtime_paths,
                owner=agent,
                name=name,
                member=agent_name,
            )
        except SharedDirectoryError as exc:
            return _payload("error", name=name, agent=agent_name, message=str(exc))
        message = (
            _changed_message(context, [agent_name], f"{agent_name} can now read it.")
            if added
            else f"{agent_name} is already a member."
        )
        return _payload("ok", name=name, agent=agent_name, members=list(directory.members), message=message)

    def _accept(self, context: ToolRuntimeContext, agent: str, owner: str, name: str) -> str:
        if (error := _answer_error(context, agent, "accepted")) is not None:
            return _payload("error", owner=owner, name=name, message=error)
        try:
            directory = accept_shared_directory_invitation(
                context.runtime_paths,
                owner=owner,
                name=name,
                agent_name=agent,
            )
        except SharedDirectoryError as exc:
            return _payload("error", owner=owner, name=name, message=str(exc))
        path = next(
            (
                str(view.path)
                for view in agent_shared_directory_views(context.config, context.runtime_paths, agent)
                if view.directory == directory
            ),
            None,
        )
        return _payload(
            "ok",
            owner=owner,
            name=name,
            path=path,
            message=_changed_message(context, [agent], "Accepted; you can now read it."),
        )

    def _decline(self, context: ToolRuntimeContext, agent: str, owner: str, name: str) -> str:
        if (error := _answer_error(context, agent, "declined")) is not None:
            return _payload("error", owner=owner, name=name, message=error)
        try:
            decline_shared_directory_invitation(context.runtime_paths, owner=owner, name=name, agent_name=agent)
        except SharedDirectoryError as exc:
            return _payload("error", owner=owner, name=name, message=str(exc))
        return _payload("ok", owner=owner, name=name, message="Declined.")

    def _remove(self, context: ToolRuntimeContext, agent: str, name: str, agent_name: str) -> str:
        try:
            before, after = remove_shared_directory_member(
                context.runtime_paths,
                owner=agent,
                name=name,
                member=agent_name,
            )
        except SharedDirectoryError as exc:
            return _payload("error", name=name, agent=agent_name, message=str(exc))
        if agent_name in before.members:
            message = _changed_message(context, [agent_name], f"{agent_name} can no longer read it.")
        elif before.pending_for(agent_name) is not None:
            message = f"The invitation to {agent_name} is cancelled."
        else:
            message = f"{agent_name} is not a member."
        return _payload("ok", name=name, agent=agent_name, members=list(after.members), message=message)

    def _delete(self, context: ToolRuntimeContext, agent: str, name: str) -> str:
        try:
            directory = delete_shared_directory(context.runtime_paths, owner=agent, name=name)
        except SharedDirectoryError as exc:
            return _payload("error", name=name, message=str(exc))
        return _payload(
            "ok",
            name=name,
            message=_changed_message(context, [directory.owner, *directory.members], "Deleted."),
        )

    def list_shared_directories(self) -> str:
        """List the shared directories you own or joined, with their paths and your access, and your pending invitations.

        Takes no arguments.
        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
        entries = [
            {
                "owner": view.directory.owner,
                "name": view.directory.name,
                "members": list(view.directory.members),
                "access": "read-write" if view.writable else "read-only",
                "path": str(view.path),
                **({"pending": _pending_entries(view.directory.pending)} if view.writable else {}),
            }
            for view in agent_shared_directory_views(context.config, context.runtime_paths, agent)
        ]
        invitations = [
            {
                "owner": directory.owner,
                "name": directory.name,
                "requested_by": invitation.requester,
                "requested_at": invitation.requested_at,
            }
            for directory, invitation in pending_shared_directory_invitations(
                context.config,
                context.runtime_paths,
                agent,
            )
        ]
        return _payload("ok", directories=entries, invitations=invitations)

    def shared_directories_write(
        self,
        action: Literal["create", "invite", "accept", "decline", "remove", "delete"],
        name: str,
        agent_name: str | None = None,
        owner: str | None = None,
    ) -> str:
        """Create, share, join, or delete shared directories.

        Actions:
        - `create`: Create a directory (`name`) you can write and later share read-only with other agents.
        - `invite`: Let `agent_name` read your shared directory (`name`) from its own worker; waits for acceptance when the user may not use `agent_name`.
        - `accept`: Accept a pending invitation from `owner` to read shared directory `name`, once the user agrees.
        - `decline`: Decline a pending invitation from `owner` to read shared directory `name`.
        - `remove`: Stop sharing your directory (`name`) with `agent_name`, or cancel its pending invitation.
        - `delete`: Delete your shared directory (`name`) and every file in it.

        Args:
            action: Operation to perform.
            name: Directory name: 1-64 lowercase letters, digits, '-' or '_', starting with a letter or digit.
            agent_name: Target agent name (required for `invite` and `remove`).
            owner: Owning agent name (required for `accept` and `decline`).

        """
        if (error := _validate_write_params(action, name, agent_name, owner)) is not None:
            return error
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
        handlers = {
            "create": lambda: self._create(context, agent, name),
            "invite": lambda: self._invite(context, agent, name, agent_name or ""),
            "accept": lambda: self._accept(context, agent, owner or "", name),
            "decline": lambda: self._decline(context, agent, owner or "", name),
            "remove": lambda: self._remove(context, agent, name, agent_name or ""),
            "delete": lambda: self._delete(context, agent, name),
        }
        return handlers[action]()
