"""Bounded MindRoom Chat UI action requests emitted through Matrix."""

from __future__ import annotations

import asyncio
import html as html_lib
import unicodedata
from typing import TYPE_CHECKING, Literal, get_args

import nio
from agno.tools import Toolkit

from mindroom.constants import UI_ACTION_CONTENT_KEY
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import entity_identity_registry
from mindroom.file_access import resolve_agent_file
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    send_message_result,
    upload_media_bytes_as_mxc,
)
from mindroom.matrix.identity import parse_historical_matrix_user_id
from mindroom.matrix.large_messages import EDIT_MESSAGE_SIZE_LIMIT, calculate_event_size
from mindroom.matrix.message_builder import build_message_content
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.models import FileAccess

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
# Counted in UTF-16 code units, the unit MindRoom Chat uses for its own title limit.
_CANVAS_TITLE_MAX_UNITS = 120
_CANVAS_TITLE_ERROR = (
    f"Canvas title must be one line of 1-{_CANVAS_TITLE_MAX_UNITS} characters of valid text without control characters."
)
# A page that fits an edit envelope travels inside the event, so every canvas stays editable;
# that plaintext ceiling also keeps the Megolm-encrypted event far below the 64 KB hard limit.
# Larger pages are uploaded as (encrypted) Matrix media and the event carries a reference.
_CANVAS_SIZE_PROBE_EVENT_ID = "$" + "x" * 64
_CANVAS_PAGE_MAX_BYTES = 4 * 1024 * 1024
# Added to the agent's instructions. The map lists only the functions the agent has (see
# ChatUITools.instructions), so include_tools or exclude_tools never advertise a missing function.
_CHAT_UI_INSTRUCTIONS = (
    "chat_ui shows parts of MindRoom Chat to the user. Each function below works on a different thing, and "
    "none of them touches the user's own computer or browser. Side panels share one place on the screen, so "
    "opening one replaces whichever is open. Each call only sends a request into the conversation: success "
    "means it was sent, not that the user saw it."
)
_FUNCTION_INSTRUCTIONS: dict[str, str] = {
    "open_panel": (
        "open_panel(panel='computer') shows the Computer panel: a live view of your own worker browser, the "
        "browser that browser_control drives with target='host'. Use it to let the user watch you on a real "
        "website, or take over, for example to log in. "
        "open_panel(panel='members') shows the Members panel: the people and agents in this room."
    ),
    "show_computer": (
        "show_computer() shows the Computer panel: a live view of your own worker browser, the browser that "
        "browser_control drives with target='host'. Use it to let the user watch you on a real website, or take "
        "over, for example to log in."
    ),
    "show_canvas": (
        "show_canvas(...) shows the Canvas panel: a web page you write yourself, which cannot load any "
        "website. Use it to present results (dashboards, reports, slides) or to let the user choose or fill "
        "something in; their answer comes back as their next message. For a quick choice between a few "
        "options, just ask in your reply."
    ),
    "open_settings": (
        "open_settings(section) opens the user's MindRoom Chat Settings dialog at one section; it changes no setting."
    ),
}


# Lines that name another function; each is added only when every function it names is enabled.
_SHOW_COMPUTER_ALIAS = "show_computer() is the same as open_panel(panel='computer')."
_REAL_WEBSITE_HINT = (
    "To show the user a real website, open it with browser_control and show the Computer panel; a canvas cannot."
)
# Only for agents whose operator says the user's Chat allows libraries; where it does not, such pages break.
_CANVAS_LIBRARIES_HINT = (
    "Canvas pages may load scripts, styles, and fonts from https://cdn.jsdelivr.net/npm/ at a pinned version, "
    'such as <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>, '
    "or as ES modules through /+esm; no other source works. Fetching data (inline it instead), workers, and "
    "code that evaluates strings, such as Alpine or Vue in-page templates, stay blocked."
)


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
    # Emoji sequences, no-break spaces, and soft hyphens are fine; line breaks, control characters,
    # and lone surrogates (which cannot be encoded) are not.
    return (
        bool(title)
        and not any(unicodedata.category(char) in {"Cc", "Cs"} or char in "\u2028\u2029" for char in title)
        and len(title.encode("utf-16-le")) // 2 <= _CANVAS_TITLE_MAX_UNITS
    )


class ChatUITools(Toolkit):
    """Ask MindRoom Chat to show the user one bounded part of its interface."""

    def __init__(
        self,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        enable_show_canvas: bool = False,
        enable_canvas_libraries: bool = False,
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        self._canvas_libraries = enable_canvas_libraries
        tools: list[Callable[..., Awaitable[str]]] = [self.show_computer, self.open_settings, self.open_panel]
        # Canvases are opt-in, like Chat's own switch, so existing chat_ui agents do not send pages
        # their users' clients refuse to show.
        if enable_show_canvas:
            tools.append(self.show_canvas)
        super().__init__(name="chat_ui", add_instructions=True, tools=tools)

    @property
    def instructions(self) -> str:
        """Map only the functions this agent has; include_tools and exclude_tools remove the others."""
        enabled = {*self.functions, *self.async_functions}
        lines = [
            _SHOW_COMPUTER_ALIAS if name == "show_computer" and "open_panel" in enabled else line
            for name, line in _FUNCTION_INSTRUCTIONS.items()
            if name in enabled
        ]
        if "show_canvas" in enabled and enabled & {"open_panel", "show_computer"}:
            lines.append(_REAL_WEBSITE_HINT)
        if "show_canvas" in enabled and self._canvas_libraries:
            lines.append(_CANVAS_LIBRARIES_HINT)
        return "\n".join([_CHAT_UI_INSTRUCTIONS, *(f"- {line}" for line in lines)])

    @instructions.setter
    def instructions(self, _value: str | None) -> None:
        """Ignore Agno's constructor assignment; the map is derived from the enabled functions."""

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
                UI_ACTION_CONTENT_KEY: metadata,
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
        """Show the user the Computer panel: a live view of your own worker browser.

        That is the browser browser_control drives with target='host'. The user starts
        out watching. They can take control, for example to log in; while they have it
        your browser calls are blocked, and when they hand it back you get a message.
        Opening the panel does not navigate, send a prompt to ChatGPT, or take control,
        and it never opens or controls the user's own browser. Success means the
        request was sent, not that the client opened the panel.
        """
        return await self._send_action(
            "show_computer",
            "Open this agent's worker computer in MindRoom Chat.",
        )

    async def open_settings(self, section: _SettingsSection = "general") -> str:
        """Open the user's MindRoom Chat Settings dialog at one section.

        This works on the Chat app's own settings screen. It changes no setting; the
        user decides what to do there. Success means the UI request was sent, not that
        the client opened Settings.
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
        """Show the user a side panel in MindRoom Chat: 'computer' or 'members'.

        panel='computer' opens the Computer panel: a live view of your own worker
        browser, the one browser_control drives with target='host'. The user starts
        out watching. They can take control, for example to log in; while they have
        it your browser calls are blocked, and when they hand it back you get a
        message. Opening the panel does not navigate to a URL, send a prompt to
        ChatGPT, or take control, and it never opens or controls the user's own
        browser: navigate first with browser_control, then open the panel.

        panel='members' opens the Members panel, listing the people and agents in
        this room.

        Success means the UI request was sent, not that the client opened a panel.

        Args:
            panel: 'computer' for your worker browser, or 'members' (the default) for this room's members.

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

    async def show_canvas(
        self,
        title: str | None = None,
        html: str | None = None,
        path: str | None = None,
        canvas_event_id: str | None = None,
    ) -> str:
        """Show the user the Canvas panel: an interactive web page you wrote, beside this conversation.

        Use a canvas when seeing or clicking beats reading or typing: dashboards,
        reports, charts, slides, menus, forms, pickers, and multi-step flows. The page
        cannot load any website, so it cannot show the user a real website. Pass the
        page as ``html``, or as a workspace-relative ``path`` (e.g. ``slides/deck.html``)
        to an HTML file, which suits pages you build and refine such as slides. Pages up
        to 4 MB are supported. Only show pages you wrote: a canvas appears as yours and
        whatever the user types into it could leave the panel.

        Design it like a polished web app. Write self-contained HTML with inline CSS
        and JavaScript. Fetch, XHR, and WebSocket requests and external images are
        blocked, and so are external scripts, styles, and fonts unless your
        instructions name a library source; otherwise draw charts with inline SVG.
        Embed images as data: URLs.
        The panel can be resized from narrow to full width, so use a responsive layout.
        Chat exposes its current theme as CSS variables so the page matches light and
        dark mode: --mr-bg, --mr-surface, --mr-surface-raised, --mr-border, --mr-text,
        --mr-text-muted, --mr-accent, --mr-accent-text, --mr-success, --mr-warning,
        --mr-danger, --mr-radius, and --mr-font; window.mindroom.colorScheme is
        'light' or 'dark' for choices the variables cannot make, such as chart colors.
        The page cannot see the user's account or messages. Pages cost output tokens:
        prefer SVG and CSS to embedded images, summarize large data instead of
        inlining it, and leave out interactivity the page does not need.

        Keep interactivity inside the page; nothing reaches you until the user
        commits. To commit, call ``window.mindroom.submit(data, {label: "short
        summary"})`` with JSON data under 512 KB, enough for a long edited text
        (decimal numbers arrive as text), or
        use a ``<form>`` (its fields are submitted automatically; its
        ``data-mindroom-label`` attribute sets the label). Chat shows the user what
        will be sent and asks them to confirm. The answer arrives as the user's next
        message, formatted as
        ``Canvas response (<canvas_event_id>, revision <event_id>): <label>``
        followed by the JSON data. If that revision is not your latest update, the user
        answered an earlier version of the page.

        If the page throws an error or loads something Chat blocks, the user can send
        you the errors as ``Canvas error (<canvas_event_id>, revision <event_id>):``
        followed by one error per line.

        To replace the page in place, for the next step of a flow or a new version of
        a file you edited, call show_canvas again with ``canvas_event_id`` set to the
        canvas ID. Success means the request was sent, not that the user opened or
        answered it.

        Args:
            title: Short single-line panel title; when updating, omit it to keep the canvas's first title.
            html: Self-contained HTML with inline CSS and JavaScript. Give html or path.
            path: Workspace-relative path of an HTML file you wrote, shown instead of html.
            canvas_event_id: Event ID of an earlier canvas from this conversation to update in place.

        """
        validated = self._validated_context("show_canvas")
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        # Models often send an empty string for an omitted optional argument.
        canvas_event_id = canvas_event_id or None
        title = title.strip() if isinstance(title, str) else ""
        page = self._canvas_input_error(title, html, path, canvas_event_id) or await self._read_canvas_page(html, path)
        if isinstance(page, str):
            return page
        target = await self._canvas_metadata(context, requester_id, canvas_event_id, title)
        if isinstance(target, str):
            return target
        metadata, title = target
        body = f"Interactive panel: {title}. Open it in MindRoom Chat to respond."
        canvas = await self._canvas_field(context, metadata, body, title, page, canvas_event_id)
        if isinstance(canvas, str):
            return canvas
        if canvas_event_id is None:
            return await self._send_validated_action(
                context,
                requester_id,
                "show_canvas",
                body,
                {"canvas": canvas},
                formatted_body=html_lib.escape(body),
            )
        return await self._update_canvas(context, canvas_event_id, body, {**metadata, "canvas": canvas})

    @classmethod
    async def _canvas_metadata(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        canvas_event_id: str | None,
        title: str,
    ) -> tuple[dict[str, object], str] | str:
        """Return the request metadata and the title; an update without one keeps the canvas's first title."""
        metadata = cls._action_metadata(context, requester_id, "show_canvas", {})
        if canvas_event_id is None:
            return metadata, title
        original = await cls._canvas_target(context, requester_id, canvas_event_id)
        if isinstance(original, str):
            return original
        # Chat accepts an edit only when its authority fields equal the original request's.
        metadata["thread_id"] = original.get("thread_id")
        match original.get("canvas"):
            case {"title": str(first_title)} if not title:
                title = first_title
        if not _canvas_title_is_valid(title):
            return cls._canvas_error(_CANVAS_TITLE_ERROR)
        return metadata, title

    @classmethod
    def _canvas_input_error(
        cls,
        title: str,
        html: object,
        path: object,
        canvas_event_id: object,
    ) -> str | None:
        if canvas_event_id is not None and (
            not isinstance(canvas_event_id, str) or not canvas_event_id.startswith("$")
        ):
            return cls._canvas_error(
                "canvas_event_id must be the event ID returned by an earlier show_canvas call.",
                canvas_event_id=canvas_event_id,
            )
        # An update may omit the title; it is checked once the canvas's own title is known.
        if (title or canvas_event_id is None) and not _canvas_title_is_valid(title):
            return cls._canvas_error(_CANVAS_TITLE_ERROR)
        given = [value for value in (html, path) if value is not None and value != ""]
        if len(given) != 1:
            return cls._canvas_error("Give exactly one of html or path.")
        if html is not None and html != "" and (not isinstance(html, str) or not html.strip()):
            return cls._canvas_error("Canvas HTML must not be empty.")
        if path is not None and path != "" and not isinstance(path, str):
            return cls._canvas_error("Canvas path must be a workspace path.")
        return None

    async def _read_canvas_page(self, html: str | None, path: str | None) -> bytes | str:
        """Return the page as UTF-8 bytes, read from the workspace when a path is given."""
        if html:
            try:
                page = html.encode("utf-8")
            except UnicodeEncodeError:
                return self._canvas_error("Canvas HTML must be valid Unicode text.")
        else:
            assert path is not None
            file_page = await self._read_canvas_file(path)
            if isinstance(file_page, str):
                return file_page
            page = file_page
        if len(page) > _CANVAS_PAGE_MAX_BYTES:
            return self._canvas_error(
                f"Canvas page is {len(page)} bytes; the limit is {_CANVAS_PAGE_MAX_BYTES}.",
            )
        return page

    async def _read_canvas_file(self, path: str) -> bytes | str:
        """Read one non-empty UTF-8 page through the agent's file access."""
        try:
            authorized = resolve_agent_file(
                path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="Canvas path",
            )
            page = await asyncio.to_thread(
                read_regular_file_within_root,
                authorized.root,
                authorized.relative,
                max_bytes=_CANVAS_PAGE_MAX_BYTES,
            )
        except (OSError, ValueError) as exc:
            message = (
                f"Canvas file is larger than the {_CANVAS_PAGE_MAX_BYTES}-byte limit."
                if "size limit" in str(exc)
                else str(exc)
            )
            return self._canvas_error(message, path=path)
        try:
            text = page.decode("utf-8")
        except UnicodeDecodeError:
            return self._canvas_error("Canvas file must be UTF-8 text.", path=path)
        if not text.strip():
            return self._canvas_error("Canvas file is empty.", path=path)
        return page

    @staticmethod
    def _canvas_replacement(body: str, metadata: dict[str, object]) -> dict[str, object]:
        """Return the notice content shared by a new canvas and an edit's m.new_content."""
        return {
            "msgtype": "m.notice",
            "body": body,
            "format": "org.matrix.custom.html",
            "formatted_body": html_lib.escape(body),
            UI_ACTION_CONTENT_KEY: metadata,
        }

    @classmethod
    async def _canvas_field(
        cls,
        context: ToolRuntimeContext,
        metadata: dict[str, object],
        body: str,
        title: str,
        page: bytes,
        canvas_event_id: str | None,
    ) -> dict[str, object] | str:
        """Carry a page inside the event when an edit of it fits, otherwise as uploaded media."""
        # Serialized JSON is never smaller than the UTF-8 page, so larger pages skip the measurement.
        if len(page) < EDIT_MESSAGE_SIZE_LIMIT:
            inline: dict[str, object] = {"title": title, "html": page.decode("utf-8")}
            probe = _canvas_edit_content(
                canvas_event_id or _CANVAS_SIZE_PROBE_EVENT_ID,
                cls._canvas_replacement(body, {**metadata, "canvas": inline}),
                body,
            )
            if calculate_event_size(probe) <= EDIT_MESSAGE_SIZE_LIMIT:
                return inline
        # Check the room's encryption trust policy first, so a refused send leaves no orphan upload.
        if not can_send_to_encrypted_room(context.client, context.room_id, operation="chat_ui_canvas_upload"):
            return cls._canvas_error("Canvas pages cannot be sent to this encrypted room.")
        # Encrypted rooms get an encrypted upload; the event carries only the reference.
        mxc_uri, upload = await upload_media_bytes_as_mxc(
            context.client,
            context.room_id,
            page,
            filename="canvas.html",
            mimetype="text/html",
        )
        if mxc_uri is None or upload is None:
            return cls._canvas_error("Failed to upload the canvas page.")
        document: dict[str, object] = {"mimetype": "text/html", "size": len(page)}
        encrypted_file = upload.get("file")
        if isinstance(encrypted_file, dict):
            document["file"] = encrypted_file
        else:
            document["url"] = mxc_uri
        return {"title": title, "document": document}

    @classmethod
    def _canvas_error(cls, message: str, **fields: object) -> str:
        return cls._payload("error", action="show_canvas", message=message, **fields)

    @classmethod
    async def _update_canvas(
        cls,
        context: ToolRuntimeContext,
        canvas_event_id: str,
        body: str,
        metadata: dict[str, object],
    ) -> str:
        """Edit one of this agent's canvases in place so the timeline keeps a single card."""
        delivered = await send_message_result(
            context.client,
            context.room_id,
            _canvas_edit_content(canvas_event_id, cls._canvas_replacement(body, metadata), body),
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
        metadata = content.get(UI_ACTION_CONTENT_KEY) if isinstance(content, dict) else None
        relation = content.get("m.relates_to") if isinstance(content, dict) else None
        if (
            response.event.sender == context.client.user_id
            and isinstance(relation, dict)
            and relation.get("rel_type") == "m.replace"
            and isinstance(relation.get("event_id"), str)
        ):
            # Answers name both IDs, so a revision is an easy mix-up; point at the canvas instead.
            original_id = relation["event_id"]
            return error(f"That ID is a revision of a canvas; pass canvas_event_id='{original_id}' instead.")
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
