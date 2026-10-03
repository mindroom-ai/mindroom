"""Bounded MindRoom Chat UI action requests emitted through Matrix."""

from __future__ import annotations

import html as html_lib
from typing import Literal, get_args

import nio
from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.identity import parse_historical_matrix_user_id
from mindroom.matrix.large_messages import EDIT_MESSAGE_SIZE_LIMIT, calculate_event_size
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
_SidePanel = Literal["members", "computer"]

_SETTINGS_SECTIONS: frozenset[str] = frozenset(get_args(_SettingsSection))
_SIDE_PANELS: frozenset[str] = frozenset(get_args(_SidePanel))
_UI_ACTION_CONTENT_KEY = "io.mindroom.ui_action"
# Counted in UTF-16 code units, the unit MindRoom Chat uses for its own title limit.
_CANVAS_TITLE_MAX_UNITS = 120
# Every canvas must stay editable, so creation and updates share the edit envelope ceiling.
# That plaintext ceiling also keeps the Megolm-encrypted event far below the 64 KB hard limit.
_CANVAS_SIZE_PROBE_EVENT_ID = "$" + "x" * 64


def _canvas_edit_content(canvas_event_id: str, replacement: dict[str, object], body: str) -> dict[str, object]:
    """Replace a canvas; the page travels once, in m.new_content, with plain text as the fallback.

    The generic edit envelope copies the replacement into the fallback, which would carry the page twice.
    """
    return {
        "msgtype": "m.notice",
        "body": f"* {body}",
        "m.new_content": replacement,
        "m.relates_to": {"rel_type": "m.replace", "event_id": canvas_event_id},
    }


def _canvas_title_is_valid(title: str) -> bool:
    return bool(title) and title.isprintable() and len(title.encode("utf-16-le")) // 2 <= _CANVAS_TITLE_MAX_UNITS


class ChatUITools(Toolkit):
    """Ask MindRoom Chat to reveal a bounded part of its interface."""

    def __init__(self) -> None:
        super().__init__(
            name="chat_ui",
            tools=[self.show_computer, self.open_settings, self.open_panel, self.show_canvas],
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

    @staticmethod
    def _action_metadata(
        context: ToolRuntimeContext,
        requester_id: str,
        action: str,
        action_fields: dict[str, object],
    ) -> dict[str, object]:
        return {
            "version": 1,
            "action": action,
            "requester_id": requester_id,
            "agent_user_id": context.client.user_id,
            "room_id": context.room_id,
            "thread_id": context.resolved_thread_id,
            **action_fields,
        }

    @classmethod
    async def _send_action(
        cls,
        action: Literal["show_computer", "open_settings", "open_panel"],
        body: str,
        **action_fields: object,
    ) -> str:
        validated = cls._validated_context(action)
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        return await cls._send_validated_action(context, requester_id, action, body, action_fields)

    @classmethod
    async def _send_validated_action(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        action: str,
        body: str,
        action_fields: dict[str, object],
        *,
        formatted_body: str | None = None,
    ) -> str:
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
        metadata = cls._action_metadata(context, requester_id, action, action_fields)
        content = build_message_content(
            body,
            formatted_body=formatted_body,
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
        """Backward-compatible alias for open_panel(panel='computer').

        Request this agent's Computer panel in watch mode. Does not navigate, send
        a prompt to ChatGPT, or take control. Success means the request was sent,
        not that the client opened the panel.
        """
        return await self._send_action(
            "show_computer",
            "Open this agent's worker computer in MindRoom Chat.",
        )

    async def open_settings(self, section: _SettingsSection = "general") -> str:
        """Request a MindRoom Chat Settings section without changing account settings.

        Success means the UI request was sent, not that the client opened Settings.
        """
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
        """Request a MindRoom Chat panel for the user: members or computer.

        Use panel='computer' to let the user watch this agent's worker browser in
        the Computer panel. It does not navigate to a URL, send a prompt to
        ChatGPT, take control, or open or control the user's local browser.
        Navigate the worker browser separately with browser_control.
        Success means the UI request was sent, not that the client opened a panel.

        Args:
            panel: 'members' for room members, or 'computer' for this agent's worker browser in watch mode.

        """
        if panel not in _SIDE_PANELS:
            return self._payload(
                "error",
                action="open_panel",
                message=f"Unsupported side panel: {panel!r}.",
            )
        if panel == "computer":
            # Keep the existing wire action and runtime checks for older clients.
            return await self.show_computer()
        return await self._send_action(
            "open_panel",
            "Open the Members panel in MindRoom Chat.",
            panel=panel,
        )

    async def show_canvas(self, title: str, html: str, canvas_event_id: str | None = None) -> str:
        """Show an interactive HTML panel (a canvas) beside this conversation in MindRoom Chat.

        Use a canvas when the user should choose, fill in, rank, or adjust something
        instead of typing: menus, forms, pickers, sliders, multi-step flows.
        Write self-contained HTML with inline CSS and JavaScript. It runs in a
        sandbox that blocks network requests and external resources (inline SVG and
        data: URLs work) and cannot see the user's account or messages.

        Keep interactivity inside the page; nothing reaches you until the user
        commits. To commit, call ``window.mindroom.submit(data, {label: "short
        summary"})`` with JSON data under 8 KB, or use a ``<form>`` (its fields are
        submitted automatically; its ``data-mindroom-label`` attribute sets the label).
        Chat shows the user what will be sent and asks them to confirm. The answer
        arrives as the user's next message, formatted as
        ``Canvas response (<canvas_event_id>, revision <event_id>): <label>``
        followed by the JSON data.

        For the next step of the same flow, call show_canvas again with
        ``canvas_event_id`` set to that canvas ID: the panel updates in place instead
        of posting another one. A canvas must fit in about 24,000 bytes of JSON: quotes,
        backslashes, and newlines count twice and non-ASCII characters count six bytes or more.
        Success means the request was sent, not that the user opened or answered it.

        Args:
            title: Short single-line panel title.
            html: Self-contained HTML with inline CSS and JavaScript.
            canvas_event_id: Event ID of an earlier canvas from this conversation to update in place.

        """
        validated = self._validated_context("show_canvas")
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        # Models often send an empty string for an omitted optional argument.
        canvas_event_id = canvas_event_id or None
        title = title.strip() if isinstance(title, str) else ""
        if input_error := self._canvas_input_error(title, html, canvas_event_id):
            return input_error
        body = f"Interactive panel: {title}. Open it in MindRoom Chat to respond."
        canvas_fields: dict[str, object] = {"canvas": {"title": title, "html": html}}
        if canvas_event_id is None:
            return await self._create_canvas(context, requester_id, body, canvas_fields)
        return await self._update_canvas(context, requester_id, canvas_event_id, body, canvas_fields)

    @classmethod
    def _canvas_input_error(cls, title: str, html: object, canvas_event_id: object) -> str | None:
        if canvas_event_id is not None and (
            not isinstance(canvas_event_id, str) or not canvas_event_id.startswith("$")
        ):
            return cls._canvas_error(
                "canvas_event_id must be the event ID returned by an earlier show_canvas call.",
                canvas_event_id=canvas_event_id,
            )
        if not _canvas_title_is_valid(title):
            return cls._canvas_error(f"Canvas title must be a single line of 1-{_CANVAS_TITLE_MAX_UNITS} characters.")
        if not isinstance(html, str) or not html.strip():
            return cls._canvas_error("Canvas HTML must not be empty.")
        return None

    @staticmethod
    def _canvas_replacement(body: str, metadata: dict[str, object]) -> dict[str, object]:
        """Return the notice content shared by a new canvas and an edit's m.new_content."""
        return {
            "msgtype": "m.notice",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": html_lib.escape(body),
            _UI_ACTION_CONTENT_KEY: metadata,
        }

    @classmethod
    async def _create_canvas(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        body: str,
        canvas_fields: dict[str, object],
    ) -> str:
        metadata = cls._action_metadata(context, requester_id, "show_canvas", canvas_fields)
        probe = _canvas_edit_content(_CANVAS_SIZE_PROBE_EVENT_ID, cls._canvas_replacement(body, metadata), body)
        if size_error := cls._canvas_size_error(probe):
            return size_error
        return await cls._send_validated_action(
            context,
            requester_id,
            "show_canvas",
            body,
            canvas_fields,
            formatted_body=html_lib.escape(body),
        )

    @classmethod
    def _canvas_error(cls, message: str, **fields: object) -> str:
        return cls._payload("error", action="show_canvas", message=message, **fields)

    @classmethod
    def _canvas_size_error(cls, edit_content: dict[str, object]) -> str | None:
        """Reject a canvas whose serialized edit would not fit, so every canvas can be updated."""
        size = calculate_event_size(edit_content)
        if size <= EDIT_MESSAGE_SIZE_LIMIT:
            return None
        return cls._canvas_error(
            f"Canvas is too large ({size} bytes as a Matrix event; the limit is {EDIT_MESSAGE_SIZE_LIMIT}). "
            "Shorten the HTML, CSS, or JavaScript and try again.",
        )

    @classmethod
    async def _update_canvas(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        canvas_event_id: str,
        body: str,
        canvas_fields: dict[str, object],
    ) -> str:
        """Edit one of this agent's canvases in place so the timeline keeps a single card."""
        metadata = cls._action_metadata(context, requester_id, "show_canvas", canvas_fields)
        # The original's thread is this thread or none, so this check never undercounts the edit.
        if size_error := cls._canvas_size_error(
            _canvas_edit_content(canvas_event_id, cls._canvas_replacement(body, metadata), body),
        ):
            return size_error
        original = await cls._canvas_target(context, requester_id, canvas_event_id)
        if isinstance(original, str):
            return original
        # Chat accepts an edit only when its authority fields equal the original request's.
        metadata["thread_id"] = original.get("thread_id")
        edit_content = _canvas_edit_content(canvas_event_id, cls._canvas_replacement(body, metadata), body)
        delivered = await send_message_result(
            context.client,
            context.room_id,
            edit_content,
            operation="chat_ui_canvas_update",
        )
        if delivered is None:
            return cls._payload(
                "error",
                action="show_canvas",
                room_id=context.room_id,
                thread_id=context.resolved_thread_id,
                message="Failed to send the canvas update.",
            )
        return cls._payload(
            "ok",
            action="show_canvas",
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
            event_id=canvas_event_id,
            revision_event_id=delivered.event_id,
            message="Canvas update sent.",
        )

    @classmethod
    async def _canvas_target(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        canvas_event_id: str,
    ) -> dict[str, object] | str:
        """Return the original request of this agent's own canvas for the same requester and room."""

        def error(message: str) -> str:
            return cls._canvas_error(message, canvas_event_id=canvas_event_id)

        response = await context.client.room_get_event(context.room_id, canvas_event_id)
        if not isinstance(response, nio.RoomGetEventResponse) or isinstance(response.event, nio.MegolmEvent):
            return error("The canvas to update could not be read in this room.")
        source = response.event.source if isinstance(response.event.source, dict) else {}
        unsigned = source.get("unsigned")
        if isinstance(unsigned, dict) and "redacted_because" in unsigned:
            return error("That canvas was deleted; call show_canvas without canvas_event_id instead.")
        content = source.get("content")
        metadata = content.get(_UI_ACTION_CONTENT_KEY) if isinstance(content, dict) else None
        relation = content.get("m.relates_to") if isinstance(content, dict) else None
        if (
            response.event.sender != context.client.user_id
            or not isinstance(content, dict)
            or content.get("msgtype") != "m.notice"
            or not isinstance(metadata, dict)
            or metadata.get("version") != 1
            or metadata.get("action") != "show_canvas"
            or metadata.get("agent_user_id") != context.client.user_id
            or (isinstance(relation, dict) and relation.get("rel_type") == "m.replace")
        ):
            return error("Only your own canvases can be updated; call show_canvas without canvas_event_id instead.")
        # A room-level canvas is answered by a reply that starts a thread, so any thread of the room may update it.
        thread_id = metadata.get("thread_id")
        if (
            metadata.get("requester_id") != requester_id
            or metadata.get("room_id") != context.room_id
            or (thread_id is not None and thread_id != context.resolved_thread_id)
        ):
            return error("That canvas belongs to another conversation; show a new canvas here instead.")
        return metadata
