"""Bounded MindRoom Chat UI action requests emitted through Matrix."""

from __future__ import annotations

from typing import Literal, get_args

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.identity import parse_historical_matrix_user_id
from mindroom.matrix.message_builder import build_message_content
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

_SettingsSection = Literal[
    "general",
    "account",
    "notifications",
    "devices",
    "emojis-stickers",
    "developer",
    "about",
]
_SidePanel = Literal["members"]

_SETTINGS_SECTIONS: frozenset[str] = frozenset(get_args(_SettingsSection))
_SIDE_PANELS: frozenset[str] = frozenset(get_args(_SidePanel))
_UI_ACTION_CONTENT_KEY = "io.mindroom.ui_action"


class ChatUITools(Toolkit):
    """Ask MindRoom Chat to reveal a bounded part of its interface."""

    def __init__(self) -> None:
        super().__init__(
            name="chat_ui",
            tools=[self.show_computer, self.open_settings, self.open_panel],
        )

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("chat_ui", status, **fields)

    @classmethod
    def _context_validation_error(cls, context: ToolRuntimeContext, action: str) -> str | None:
        if context.agent_name not in context.current_config.agents:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI actions require a configured agent identity; team and router contexts are unsupported.",
            )
        transport_agent_name = context.transport_agent_name or context.agent_name
        if transport_agent_name != context.agent_name:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI actions do not support delegated transport identity mismatches.",
            )
        if not context.room_id.startswith("!") or ":" not in context.room_id:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action room context is not a valid Matrix room ID.",
            )
        if context.resolved_thread_id is not None and not context.resolved_thread_id.startswith("$"):
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action thread context is not a valid canonical Matrix event ID.",
            )
        if context.reply_to_event_id is not None and not context.reply_to_event_id.startswith("$"):
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action reply context is not a valid Matrix event ID.",
            )
        return None

    @staticmethod
    def _expected_sender(context: ToolRuntimeContext) -> str | None:
        try:
            return (
                entity_identity_registry(
                    context.current_config,
                    context.runtime_paths,
                )
                .current_id(context.agent_name)
                .full_id
            )
        except (KeyError, RuntimeError, ValueError):
            return None

    @classmethod
    def _validated_context(cls, action: str) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI tool runtime context is unavailable in this runtime path.",
            )
        if validation_error := cls._context_validation_error(context, action):
            return validation_error
        try:
            requester_id = parse_historical_matrix_user_id(context.requester_id)
        except (TypeError, ValueError):
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action requester identity is not a valid Matrix user ID.",
            )
        expected_sender = cls._expected_sender(context)
        if expected_sender is None:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action agent identity is unavailable from the managed Matrix runtime.",
            )
        sender = context.client.user_id
        if not isinstance(sender, str) or sender != expected_sender:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI action configured agent identity does not match the Matrix sender.",
            )
        return context, requester_id

    @classmethod
    async def _send_action(
        cls,
        action: Literal["show_computer", "open_settings", "open_panel"],
        body: str,
        **action_fields: str,
    ) -> str:
        validated = cls._validated_context(action)
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        thread_id = context.resolved_thread_id
        latest_thread_event_id = context.reply_to_event_id
        if thread_id is not None and latest_thread_event_id is None:
            latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
                room_id=context.room_id,
                thread_id=thread_id,
            )
            if latest_thread_event_id is None:
                return cls._payload(
                    "error",
                    action=action,
                    room_id=context.room_id,
                    thread_id=thread_id,
                    message="Failed to resolve Matrix thread fallback for UI action request.",
                )
        metadata: dict[str, object] = {
            "version": 1,
            "action": action,
            "requester_id": requester_id,
            "agent_user_id": context.client.user_id,
            "room_id": context.room_id,
            "thread_id": thread_id,
            **action_fields,
        }
        content = build_message_content(
            body,
            thread_event_id=thread_id,
            reply_to_event_id=context.reply_to_event_id if thread_id is not None else None,
            latest_thread_event_id=latest_thread_event_id if thread_id is not None else None,
            extra_content={
                "msgtype": "m.notice",
                _UI_ACTION_CONTENT_KEY: metadata,
            },
        )
        delivered = await send_message_result(
            context.client,
            context.room_id,
            content,
            operation="chat_ui_action",
        )
        if delivered is None:
            return cls._payload(
                "error",
                action=action,
                room_id=context.room_id,
                thread_id=thread_id,
                message="Failed to send the UI action request.",
            )
        return cls._payload(
            "ok",
            action=action,
            room_id=context.room_id,
            thread_id=thread_id,
            event_id=delivered.event_id,
            message="UI action request sent.",
        )

    async def show_computer(self) -> str:
        """Ask MindRoom Chat to show this agent's worker computer."""
        return await self._send_action(
            "show_computer",
            "Open this agent's worker computer in MindRoom Chat.",
        )

    async def open_settings(self, section: _SettingsSection = "general") -> str:
        """Ask MindRoom Chat to open one supported Settings section."""
        if section not in _SETTINGS_SECTIONS:
            return self._payload(
                "error",
                action="open_settings",
                message=f"Unsupported settings section: {section!r}.",
            )
        return await self._send_action(
            "open_settings",
            f"Open Settings ({section}) in MindRoom Chat.",
            section=section,
        )

    async def open_panel(self, panel: _SidePanel = "members") -> str:
        """Ask MindRoom Chat to open one supported conversation side panel."""
        if panel not in _SIDE_PANELS:
            return self._payload(
                "error",
                action="open_panel",
                message=f"Unsupported side panel: {panel!r}.",
            )
        return await self._send_action(
            "open_panel",
            "Open the Members panel in MindRoom Chat.",
            panel=panel,
        )
