"""Agent manager tools: create agents, and let an owner read, change and delete the agents generated for them."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any, Literal

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
from mindroom.tool_system.runtime_context import require_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from mindroom.tools.native_helpers import require_enabled_action, restrict_actions, validate_action_params

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_WRITE_ACTION_FIELDS: dict[str, str] = {
    "create": "name display_name role instructions tools",
    "update": "changes",
    "set_avatar": "attachment_id",
    "delete": "",
}
_WRITE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "create": ("name", "display_name", "role"),
    "update": (),
    "set_avatar": ("attachment_id",),
    "delete": (),
}


class AgentManagerTools(Toolkit):
    """Tools to create agents and to manage this agent when it is generated for an owner."""

    def __init__(self, agent_name: str, *, can_create: bool, generated: bool, created: bool) -> None:
        self.agent_name = agent_name
        self._write_actions: tuple[str, ...] = (
            *(("create",) if can_create else ()),
            *(("update", "set_avatar") if generated else ()),
            *(("delete",) if created else ()),
        )
        tools = [
            *([self.agent_settings] if generated else []),
            *([self.agent_manager_write] if self._write_actions else []),
        ]
        super().__init__(name="agent_manager", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Require confirmation of each write and narrow schema and docs to enabled write actions."""
        # The owner confirms each change, because the model issuing the call also reads untrusted content.
        if function.name == "agent_manager_write":
            confirmed = replace(function, gate=ToolGate.CONFIRM)
            return restrict_actions(confirmed, self._write_actions, action_fields=_WRITE_ACTION_FIELDS)
        return function

    async def _run(self, operation: Callable[..., Any], *, reload: bool = True, **extra: object) -> Any:  # noqa: ANN401
        """Run one operation for the requester, off the event loop if it blocks, and request a reload after a change."""
        context = require_tool_runtime_context()
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

    async def agent_settings(self) -> str:
        """Show this agent's editable settings and the toolkit names `tools` may use (`allowed_tools`).

        Takes no arguments. Call it before `agent_manager_write(action="update", ...)`. Only this agent's owners may ask.
        """
        try:
            settings = await self._run(generated_agent_settings, reload=False, agent_name=self.agent_name)
        except ValueError as exc:
            return f"Error: {exc}"
        return yaml.safe_dump(settings, sort_keys=False)

    async def agent_manager_write(
        self,
        action: Literal["create", "update", "set_avatar", "delete"],
        name: str | None = None,
        display_name: str | None = None,
        role: str | None = None,
        instructions: list[str] | None = None,
        tools: list[str] | None = None,
        changes: dict[str, str | list[str] | None] | None = None,
        attachment_id: str | None = None,
    ) -> str:
        """Create an agent or change, re-avatar, or delete this generated agent.

        The owner approves on a card unless their own /auto_approve window is open.

        Actions:
        - `create`: Create an agent for the person asking with `name`, `display_name`, `role`, and optional `instructions` and `tools` (toolkit names you have, such as `calculator`, never function names; omit to inherit yours).
        - `update`: Change this agent's `display_name`, `role`, `instructions`, or `tools` (plus `co_owners` for a created agent) via `changes`; call `agent_settings` first, send only changed fields, and pass null to restore the base value.
        - `set_avatar`: Set this agent's Matrix profile picture from an attached PNG, JPEG, GIF, or WebP image (`attachment_id`, such as `att_...`), cropped to its center square.
        - `delete`: Delete this created agent (only its owner may do this; its Matrix account stays, so the name stays taken).
        """
        if (err := require_enabled_action(action, self._write_actions)) is not None:
            return f"Error: {err}"
        params: dict[str, object] = {
            "name": name,
            "display_name": display_name,
            "role": role,
            "instructions": instructions,
            "tools": tools,
            "changes": changes,
            "attachment_id": attachment_id,
        }
        if error := validate_action_params(
            None,
            action,
            _WRITE_ACTION_FIELDS,
            params,
            required_by_action=_WRITE_REQUIRED_FIELDS,
        ):
            return f"Error: {error}"
        return await self._dispatch_write(
            action,
            name=name,
            display_name=display_name,
            role=role,
            instructions=instructions,
            tools=tools,
            changes=changes,
            attachment_id=attachment_id,
        )

    async def _dispatch_write(
        self,
        action: Literal["create", "update", "set_avatar", "delete"],
        *,
        name: str | None,
        display_name: str | None,
        role: str | None,
        instructions: list[str] | None,
        tools: list[str] | None,
        changes: dict[str, str | list[str] | None] | None,
        attachment_id: str | None,
    ) -> str:
        if action == "update":
            if not changes:
                return "Error: no changes given"
            try:
                await self._run(update_generated_agent, agent_name=self.agent_name, changes=changes)
            except ValueError as exc:
                return f"Error: {config_validation_summary(exc)}"
            return f"Updated {self.agent_name}: {', '.join(changes)}."
        try:
            if action == "create" and name is not None and display_name is not None and role is not None:
                user_id = await self._run(
                    provision_created_agent,
                    creator=self.agent_name,
                    name=name,
                    display_name=display_name,
                    role=role,
                    instructions=instructions,
                    tools=tools,
                )
                message = (
                    f"Created {user_id}. Invite {user_id} to a room to talk to it; it accepts invites from its owners."
                )
            elif action == "set_avatar" and attachment_id is not None:
                context = require_tool_runtime_context()
                image = await self._attached_image(context, attachment_id)
                await self._run(
                    set_generated_agent_avatar,
                    reload=False,
                    agent_name=self.agent_name,
                    client=context.client,
                    image=image,
                )
                message = f"Set the avatar of {self.agent_name}."
            else:
                await self._run(delete_created_agent, agent_name=self.agent_name)
                message = f"Deleted {self.agent_name}."
        except ValueError as exc:
            return f"Error: {exc}"
        return message

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
