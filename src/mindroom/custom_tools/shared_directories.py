"""Tools for sharing a directory with other agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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


class SharedDirectoryTools(Toolkit):
    """Create directories other agents can read from their own workers."""

    def __init__(self, agent_name: str | None = None) -> None:
        # The calling agent, bound when the tool is built, so a team member shares as itself rather than as the team.
        self._agent_name = agent_name
        super().__init__(
            name="shared_directories",
            tools=[
                self.create_shared_directory,
                self.invite_to_shared_directory,
                self.remove_from_shared_directory,
                self.delete_shared_directory,
                self.list_shared_directories,
                self.accept_shared_directory,
                self.decline_shared_directory,
            ],
        )

    def _context(self) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None or self._agent_name is None:
            return _payload("error", message="Shared directory tool context is unavailable in this runtime path.")
        if not is_shareable_agent(context.config, self._agent_name):
            return _payload("error", message="Private agents cannot share directories.")
        return context, self._agent_name

    def create_shared_directory(self, name: str) -> str:
        """Create a directory you can write and later share read-only with other agents.

        Args:
            name: Directory name: 1-64 lowercase letters, digits, '-' or '_', starting with a letter or digit.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
        try:
            directory = create_shared_directory(context.runtime_paths, owner=agent, name=name)
        except SharedDirectoryError as exc:
            return _payload("error", name=name, message=str(exc))
        return _payload(
            "ok",
            name=name,
            path=next(
                str(view.path)
                for view in agent_shared_directory_views(context.config, context.runtime_paths, agent)
                if view.directory == directory
            ),
            message=_changed_message(context, [agent], "Created."),
        )

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

    def invite_to_shared_directory(self, name: str, agent_name: str) -> str:
        """Let another agent read one of your shared directories from its own worker.

        When the user you act for may not use that agent, the invitation waits until a user who may use it
        accepts it through that agent.

        Args:
            name: Name of a directory you created.
            agent_name: Name of the agent to invite.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
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
        return _payload(
            "ok",
            name=name,
            agent=agent_name,
            members=list(directory.members),
            message=_changed_message(context, [agent_name], f"{agent_name} can now read it.")
            if added
            else f"{agent_name} is already a member.",
        )

    def accept_shared_directory(self, owner: str, name: str) -> str:
        """Accept an invitation to read another agent's shared directory, once the user you act for agrees.

        Args:
            owner: Agent that owns the directory.
            name: Name of the directory.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
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
        return _payload(
            "ok",
            owner=owner,
            name=name,
            path=next(
                (
                    str(view.path)
                    for view in agent_shared_directory_views(context.config, context.runtime_paths, agent)
                    if view.directory == directory
                ),
                None,
            ),
            message=_changed_message(context, [agent], "Accepted; you can now read it."),
        )

    def decline_shared_directory(self, owner: str, name: str) -> str:
        """Decline an invitation to read another agent's shared directory.

        Args:
            owner: Agent that owns the directory.
            name: Name of the directory.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
        if (error := _answer_error(context, agent, "declined")) is not None:
            return _payload("error", owner=owner, name=name, message=error)
        try:
            decline_shared_directory_invitation(context.runtime_paths, owner=owner, name=name, agent_name=agent)
        except SharedDirectoryError as exc:
            return _payload("error", owner=owner, name=name, message=str(exc))
        return _payload("ok", owner=owner, name=name, message="Declined.")

    def remove_from_shared_directory(self, name: str, agent_name: str) -> str:
        """Stop sharing one of your directories with an agent.

        Args:
            name: Name of a directory you created.
            agent_name: Name of the member to remove.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
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

    def delete_shared_directory(self, name: str) -> str:
        """Delete one of your shared directories and every file in it.

        Args:
            name: Name of a directory you created.

        """
        resolved = self._context()
        if isinstance(resolved, str):
            return resolved
        context, agent = resolved
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
        """List the shared directories you own or joined, with their paths and your access, and your pending invitations."""
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
