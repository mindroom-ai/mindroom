"""Agent manager tools: create agents, and let an owner read, change and delete the agents generated for them."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

import yaml

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.config.main import config_validation_summary
from mindroom.custom_tools.attachments import read_context_attachment
from mindroom.media_delivery import MAX_SOURCE_BYTES
from mindroom.personal_agents import (
    delete_created_agent,
    generated_agent_settings,
    provision_created_agent,
    set_generated_agent_avatar,
    update_generated_agent,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction


def _require_context() -> ToolRuntimeContext:
    """Return the live request's context, refusing a call made outside one."""
    context = get_tool_runtime_context()
    if context is None:
        msg = "This tool only works while answering a request, and there is none."
        raise ValueError(msg)
    return context


class AgentManagerTools(Toolkit):
    """Tools to create agents and to manage this agent when it is generated for an owner."""

    def __init__(self, agent_name: str, *, can_create: bool, generated: bool, created: bool) -> None:
        self.agent_name = agent_name
        changes = [
            *([self.create_agent] if can_create else []),
            *([self.update_agent, self.set_avatar] if generated else []),
            *([self.delete_agent] if created else []),
        ]
        self._changes = frozenset(change.__name__ for change in changes)
        super().__init__(name="agent_manager", tools=[*([self.agent_settings] if generated else []), *changes])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require confirmation of each change."""
        # The owner confirms each change, because the model issuing the call also reads untrusted content.
        if function.name in self._changes:
            return replace(function, gate=ToolGate.CONFIRM)
        return function

    async def _run(self, operation: Callable[..., Any], *, reload: bool = True, **extra: object) -> Any:  # noqa: ANN401
        """Run one operation for the requester, off the event loop if it blocks, and request a reload after a change."""
        context = _require_context()
        call = partial(
            operation,
            context.current_config,
            context.runtime_paths,
            requester_id=context.requester_id,
            **extra,
        )
        result = await call() if iscoroutinefunction(operation) else await run_blocking_until_complete(call)
        if reload and context.orchestrator is not None:
            context.orchestrator.config_reload.request_reload()
        return result

    async def create_agent(
        self,
        name: str,
        display_name: str,
        role: str,
        instructions: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> str:
        """Create an agent for the person asking, with a role of its own and some of your tools.

        The new agent's ID is your name plus `name`. `instructions` replace yours; omit them to keep yours.
        `tools` are toolkit names you have, such as `calculator`, never function names; omit it to give all of yours.
        The owner approves on a card unless their own !auto_approve window is open;
        on "Tool approval needs the router in this room", call `invite_router` and retry.
        """
        try:
            user_id = await self._run(
                provision_created_agent,
                creator=self.agent_name,
                name=name,
                display_name=display_name,
                role=role,
                instructions=instructions,
                tools=tools,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Created {user_id}. Invite {user_id} to a room to talk to it; it accepts invites from its owners."

    async def agent_settings(self) -> str:
        """Show this agent's editable settings and the toolkit names `tools` may use (`allowed_tools`).

        Call it before `update_agent`. Only this agent's owners may ask.
        """
        try:
            settings = await self._run(generated_agent_settings, reload=False, agent_name=self.agent_name)
        except ValueError as exc:
            return f"Error: {exc}"
        return yaml.safe_dump(settings, sort_keys=False)

    async def update_agent(self, changes: dict[str, str | list[str] | None]) -> str:
        """Change this agent's display_name, role, instructions or tools; a created agent's owner may also change co_owners.

        Call `agent_settings` first. Send only the fields you change: each value replaces the whole field,
        and null restores the base agent's value. `tools` are toolkit names from `allowed_tools`, never function names.
        The owner approves on a card unless their own !auto_approve window is open;
        on "Tool approval needs the router in this room", call `invite_router` and retry.
        """
        if not changes:
            return "Error: no changes given"
        try:
            await self._run(update_generated_agent, agent_name=self.agent_name, changes=changes)
        except ValueError as exc:
            return f"Error: {config_validation_summary(exc)}"
        return f"Updated {self.agent_name}: {', '.join(changes)}."

    async def set_avatar(self, attachment_id: str) -> str:
        """Set this agent's Matrix profile picture to an image attached in this conversation, cropped to its center square.

        Pass the attachment ID, such as `att_...`, of a PNG, JPEG, GIF or WebP image. Only this agent's owners may ask.
        The owner approves on a card unless their own !auto_approve window is open;
        on "Tool approval needs the router in this room", call `invite_router` and retry.
        """
        try:
            context = _require_context()
            image = await self._attached_image(context, attachment_id)
            await self._run(
                set_generated_agent_avatar,
                reload=False,
                agent_name=self.agent_name,
                client=context.client,
                image=image,
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Set the avatar of {self.agent_name}."

    async def _attached_image(self, context: ToolRuntimeContext, attachment_id: str) -> bytes:
        """Read an attached image for this agent's avatar, refusing a turn it answers through another account."""
        # The client is the account answering this turn, which for a team member is the team's.
        if context.recipient != self.agent_name:
            msg = f"{self.agent_name} can change its avatar only when it answers as itself, not through {context.recipient}"
            raise ValueError(msg)
        image, error = await asyncio.to_thread(
            read_context_attachment,
            context,
            attachment_id,
            byte_limit=MAX_SOURCE_BYTES,
            limit_label="avatar",
        )
        if image is None:
            raise ValueError(error)
        return image

    async def delete_agent(self) -> str:
        """Delete this agent, which only its owner may do and only for an agent made with `create_agent`.

        Its Matrix account stays, so the name stays taken.
        The owner approves on a card unless their own !auto_approve window is open;
        on "Tool approval needs the router in this room", call `invite_router` and retry.
        """
        try:
            await self._run(delete_created_agent, agent_name=self.agent_name)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Deleted {self.agent_name}."
