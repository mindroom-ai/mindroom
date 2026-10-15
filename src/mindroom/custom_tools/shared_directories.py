"""Tools for sharing a directory with other agents."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.requester_identity import is_access_checked_requester_id
from mindroom.shared_directories.paths import agent_reads_shares_in_worker, shared_directory_view_path
from mindroom.shared_directories.store import (
    SharedDirectoryError,
    add_shared_directory_member,
    agent_shared_directories,
    create_shared_directory,
    delete_shared_directory,
    is_shareable_agent,
    remove_shared_directory_member,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.shared_directories.store import SharedDirectory
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _payload(status: str, **fields: object) -> str:
    return custom_tool_payload("shared_directories", status, **fields)


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


class SharedDirectoryTools(Toolkit):
    """Create directories other agents can read from their own workers."""

    def __init__(self) -> None:
        super().__init__(
            name="shared_directories",
            tools=[
                self.create_shared_directory,
                self.invite_to_shared_directory,
                self.remove_from_shared_directory,
                self.delete_shared_directory,
                self.list_shared_directories,
            ],
        )

    @staticmethod
    def _context() -> ToolRuntimeContext | str:
        context = get_tool_runtime_context()
        if context is None:
            return _payload("error", message="Shared directory tool context is unavailable in this runtime path.")
        if not is_shareable_agent(context.config, context.agent_name):
            return _payload("error", message="Private agents cannot share directories.")
        return context

    @staticmethod
    def _view_path(context: ToolRuntimeContext, directory: SharedDirectory) -> str:
        in_worker = agent_reads_shares_in_worker(context.config, context.runtime_paths, context.agent_name)
        return str(shared_directory_view_path(context.runtime_paths, directory, in_worker=in_worker))

    def create_shared_directory(self, name: str) -> str:
        """Create a directory you can write and later share read-only with other agents.

        Args:
            name: Directory name: 1-64 lowercase letters, digits, '-' or '_'.

        """
        context = self._context()
        if isinstance(context, str):
            return context
        try:
            directory = create_shared_directory(
                context.runtime_paths,
                owner=context.agent_name,
                name=name,
                now=datetime.now(UTC),
            )
        except SharedDirectoryError as exc:
            return _payload("error", name=name, message=str(exc))
        return _payload(
            "ok",
            name=name,
            path=self._view_path(context, directory),
            message=_changed_message(context, [context.agent_name], "Created."),
        )

    def _invitation_error(self, context: ToolRuntimeContext, agent_name: str) -> str | None:
        config = context.config
        if agent_name not in config.agents:
            return f"'{agent_name}' is not an agent."
        if agent_name == context.agent_name:
            return "You cannot invite yourself."
        if not is_shareable_agent(config, agent_name):
            return f"{agent_name} is private and cannot join shared directories."
        if not is_access_checked_requester_id(context.requester_id, config, context.runtime_paths):
            return "Agents can only be invited on behalf of a user."
        if not is_sender_allowed_for_responder(
            context.requester_id,
            agent_name,
            None,
            config,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
        ):
            return f"The user you act for may not use {agent_name}, so it cannot be invited."
        return None

    def invite_to_shared_directory(self, name: str, agent_name: str) -> str:
        """Let another agent read one of your shared directories from its own worker.

        Args:
            name: Name of a directory you created.
            agent_name: Name of the agent to invite.

        """
        context = self._context()
        if isinstance(context, str):
            return context
        if (error := self._invitation_error(context, agent_name)) is not None:
            return _payload("error", name=name, agent=agent_name, message=error)
        try:
            directory, added = add_shared_directory_member(
                context.runtime_paths,
                owner=context.agent_name,
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

    def remove_from_shared_directory(self, name: str, agent_name: str) -> str:
        """Stop sharing one of your directories with an agent.

        Args:
            name: Name of a directory you created.
            agent_name: Name of the member to remove.

        """
        context = self._context()
        if isinstance(context, str):
            return context
        try:
            directory, removed = remove_shared_directory_member(
                context.runtime_paths,
                owner=context.agent_name,
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
            message=_changed_message(context, [agent_name], f"{agent_name} can no longer read it.")
            if removed
            else f"{agent_name} is not a member.",
        )

    def delete_shared_directory(self, name: str) -> str:
        """Delete one of your shared directories and every file in it.

        Args:
            name: Name of a directory you created.

        """
        context = self._context()
        if isinstance(context, str):
            return context
        try:
            directory = delete_shared_directory(context.runtime_paths, owner=context.agent_name, name=name)
        except SharedDirectoryError as exc:
            return _payload("error", name=name, message=str(exc))
        return _payload(
            "ok",
            name=name,
            message=_changed_message(context, [directory.owner, *directory.members], "Deleted."),
        )

    def list_shared_directories(self) -> str:
        """List the shared directories you own or were invited to, with their paths and your access."""
        context = self._context()
        if isinstance(context, str):
            return context
        entries = [
            {
                "owner": directory.owner,
                "name": directory.name,
                "members": list(directory.members),
                "access": "read-write" if directory.owner == context.agent_name else "read-only",
                "path": self._view_path(context, directory),
            }
            for directory in agent_shared_directories(context.runtime_paths, context.config, context.agent_name)
        ]
        return _payload("ok", directories=entries)
