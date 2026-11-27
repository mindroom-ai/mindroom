"""Bounded MindRoom Chat UI action requests emitted through Matrix."""

from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import json
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, get_args

import nio
from nio.api import RelationshipType

from mindroom.constants import UI_ACTION_CONTENT_KEY
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import entity_identity_registry
from mindroom.file_access import resolve_agent_file
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import (
    can_send_to_encrypted_room,
    send_message_result,
    upload_media_bytes_as_mxc,
)
from mindroom.matrix.identity import parse_historical_matrix_user_id
from mindroom.matrix.large_messages import EDIT_MESSAGE_SIZE_LIMIT, calculate_event_size
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.path_confinement import read_regular_file_within_root
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection
    from pathlib import Path

    from mindroom.config.models import FileAccess
    from mindroom.tool_system.tool_function import ToolFunction

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
# The libraries MindRoom Chat serves to canvas pages itself.
_CanvasLibrary = Literal["chartjs", "d3", "mermaid", "katex"]

logger = get_logger(__name__)

_SHOW_COMPUTER_BODY = "Open this agent's worker computer in MindRoom Chat."


@dataclass
class _ComputerNotice:
    """One conversation's show_computer notice: serialized sends and the reply that delivered it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delivered: bool = False
    reply: str | None = None


# Conversations, keyed by agent Matrix user, requester, room, and thread (None for the room timeline),
# whose show_computer notice was sent or attempted in this process, so the agent's first browser use
# announces it only once and one reply (correlation ID) never sends it twice. Each requester has their
# own computer and Chat shows only notices addressed to its own user.
_SHOWN_COMPUTERS: dict[tuple[str, str, str, str | None], _ComputerNotice] = {}
# Counted in UTF-16 code units, the unit MindRoom Chat uses for its own title limit.
_CANVAS_TITLE_MAX_UNITS = 120
_CANVAS_TITLE_ERROR = (
    f"Canvas title must be one line of 1-{_CANVAS_TITLE_MAX_UNITS} characters of valid text without control characters."
)
_CANVAS_EVENT_ID_ERROR = "canvas_event_id must be the event ID returned by an earlier show_canvas call."
# A page that fits an edit envelope travels inside the event, so every canvas stays editable;
# that plaintext ceiling also keeps the Megolm-encrypted event far below the 64 KB hard limit.
# Larger pages are uploaded as (encrypted) Matrix media and the event carries a reference.
_CANVAS_SIZE_PROBE_EVENT_ID = "$" + "x" * 64
_CANVAS_PAGE_MAX_BYTES = 4 * 1024 * 1024
# Chat shares a canvas's state as this event, a reference to the canvas, so one relations call finds the newest.
_CANVAS_STATE_EVENT_TYPE = "io.mindroom.canvas_state"
_CANVAS_STATE_SCAN_LIMIT = 50
# Cross-function Chat UI rules; per-function details live on the function docstrings.
_CHAT_UI_INSTRUCTIONS = (
    "chat_ui only affects MindRoom Chat (never the user's computer/browser). Side panels share one slot."
)


# Added only when the agent has both show_canvas and open_panel.
_REAL_WEBSITE_HINT = (
    "To show a real website, open it in your worker browser (first call shows the Computer panel); a canvas cannot."
)
# Only for agents whose operator says the user's Chat allows libraries; where it does not, such pages break.
_CANVAS_LIBRARIES_HINT = (
    "For libraries beyond show_canvas's bundled list, canvas pages may load pinned scripts/styles/fonts from "
    "https://cdn.jsdelivr.net/npm/ (e.g. leaflet@1.9.4/dist/leaflet.js or /+esm); no other source works. "
    "Data fetches (inline data instead), workers, and string-eval (e.g. Alpine/Vue templates) stay blocked."
)

_CANVAS_REMOTE_ASSETS_HINT = (
    "Canvas pages may load images, styles, fonts, media, and scripts from any https: address (e.g. real site assets); "
    "data fetches, workers, and nested frames stay blocked."
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


def _computer_notice(context: ToolRuntimeContext, requester_id: str) -> _ComputerNotice:
    conversation = (context.client.user_id, requester_id, context.room_id, context.resolved_thread_id)
    return _SHOWN_COMPUTERS.setdefault(conversation, _ComputerNotice())


def _canvas_title_is_valid(title: str) -> bool:
    # Emoji sequences, no-break spaces, and soft hyphens are fine; line breaks, control characters,
    # and lone surrogates (which cannot be encoded) are not.
    return (
        bool(title)
        and not any(unicodedata.category(char) in {"Cc", "Cs"} or char in "\u2028\u2029" for char in title)
        and len(title.encode("utf-16-le")) // 2 <= _CANVAS_TITLE_MAX_UNITS
    )


def _canvas_presentation_error(width: int | None, libraries: list[_CanvasLibrary] | None) -> str | None:
    if width is not None and (isinstance(width, bool) or not isinstance(width, int) or not 320 <= width <= 2560):
        return "Canvas width must be an integer from 320 to 2560 (not a boolean)."
    known = get_args(_CanvasLibrary)
    if libraries is not None and (not isinstance(libraries, list) or any(name not in known for name in libraries)):
        return f"Canvas libraries must be a list of: {', '.join(known)}."
    return None


class ChatUITools(Toolkit):
    """Ask MindRoom Chat to show the user one bounded part of its interface."""

    offer_output_path = False

    def __init__(
        self,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        enable_show_canvas: bool = False,
        enable_canvas_libraries: bool = False,
        enable_canvas_remote_assets: bool = False,
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        self._canvas_libraries = enable_canvas_libraries
        self._canvas_remote_assets = enable_canvas_remote_assets
        tools: list[Callable[..., Awaitable[str]]] = [self.open_settings, self.open_panel]
        # Canvases are opt-in, like Chat's own switch, so existing chat_ui agents do not send pages
        # their users' clients refuse to show.
        if enable_show_canvas:
            tools.extend([self.show_canvas, self.read_canvas_state])
        super().__init__(name="chat_ui", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Allow redirecting canvas state reads while keeping UI actions lean."""
        if function.name == "read_canvas_state":
            return replace(function, offer_output_path=True)
        return function

    @property
    def instructions(self) -> str:  # ty: ignore[incompatible-property-override]  # Toolkit writes it only when passed one
        """Map the built functions; ``with_offered_instructions`` narrows them to the ones an agent is offered."""
        return self.instructions_for(self.functions)

    def instructions_for(self, function_names: Collection[str]) -> str:
        """Return cross-function Chat UI rules and enabled dynamic hints for ``function_names``."""
        enabled = set(function_names)
        lines: list[str] = []
        if {"show_canvas", "open_panel"} <= enabled:
            lines.append(_REAL_WEBSITE_HINT)
        # Remote assets include the library source, and the libraries hint says no other source works.
        if "show_canvas" in enabled and self._canvas_remote_assets:
            lines.append(_CANVAS_REMOTE_ASSETS_HINT)
        elif "show_canvas" in enabled and self._canvas_libraries:
            lines.append(_CANVAS_LIBRARIES_HINT)
        if not lines:
            return _CHAT_UI_INSTRUCTIONS
        return "\n".join([_CHAT_UI_INSTRUCTIONS, *(f"- {line}" for line in lines)])

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
        if context.recipient != context.agent_name:
            return cls._payload(
                "error",
                action=action,
                message="Chat UI actions do not support delegated transport identity mismatches.",
            )
        if not context.room_id.startswith("!"):  # room version 12 IDs carry no ":server" part
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
        if sender != expected_sender:
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
        action: Literal["open_settings", "open_panel"],
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

    async def _show_computer(self) -> str:
        """Send the show_computer notice for open_panel(panel='computer'), at most once per reply."""
        validated = self._validated_context("show_computer")
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        notice = _computer_notice(context, requester_id)
        async with notice.lock:
            if notice.delivered and context.correlation_id is not None and notice.reply == context.correlation_id:
                # This reply's first browser call, or an earlier Computer panel request, already delivered the notice.
                return self._payload(
                    "ok",
                    action="show_computer",
                    message="A Computer panel request was already sent in this reply.",
                )
            return await _send_computer_notice(context, requester_id, notice)

    async def open_settings(self, section: _SettingsSection = "general") -> str:
        """Open MindRoom Chat Settings at one section. Changes no setting (user decides). Success = request sent, not opened."""
        return await self._send_action(
            "open_settings",
            f"Open Settings ({section}) in MindRoom Chat.",
            section=section,
        )

    async def open_panel(self, panel: _SidePanel = "members") -> str:
        """Show a MindRoom Chat side panel: computer or members.

        computer: live worker browser view (browser_read/browser_control target=host); user may take control (e.g. login), blocking your browser calls until handback. Never navigates, takes control, or opens the user's browser. First host browser_read/browser_control or browser_mcp call auto-shows it; reopen only in a later reply (max 1/reply). members: room people/agents. Success = request sent, not opened.

        Args:
            panel: computer (worker browser) or members (default).

        """
        if panel == "computer":
            # Chat reads the Computer panel request as its own show_computer wire action.
            return await self._show_computer()
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
        share_state: bool = False,
        inline: bool = False,
        raw: bool = False,
        width: int | None = None,
        libraries: list[_CanvasLibrary] | None = None,
    ) -> str:
        """Create/replace HTML canvas you wrote (charts, diagrams, mockups, pickers, forms, dashboards, slides) inline or panel.

        New: title + html or path. Replace: original canvas_event_id + html or path. Reopen unchanged: no call; user picks in room header.
        Use when seeing/clicking beats text (quick choice: ask in reply). Prefer show_canvas over ASCII art; use ```mermaid fences for simple inline diagrams.

        Layout: prefer inline=true (~700px desktop/360px phone) for in-flow/multi-step flows; panel (default) for persistent views/slides/long docs. New canvases appear below your reply text (refer as below/title).
        Inline themed: html/body has no background/card/title banner/side padding/vh/height:100%; fixed px chart heights; keep forms themed (no raw/width); scroll-away restarts (use id/name or saveState); next step: update or new.
        Site/app preview: inline=true, raw=true (no Chat styles/vars), width (320-2560, e.g. 1440/390, scaled; overrides inline rules; <=16:10, 568-1080px, vh/scroll work; panel uses own width).

        Sandbox & theme: responsive, SVG/CSS over images, summarize large data, skip needless interactivity, <=4 MB; only your HTML (appears as yours; typed data could leave; no real websites). Blocked: fetch/XHR/WebSockets, user data, external assets unless instructions allow (embed data: images; pass libraries for Chart.js/D3/Mermaid/KaTeX, never CDN).
        Vars (--mr-*, e.g. var(--mr-bg)): bg, surface, surface-raised, border, text, text-muted, accent, accent-text, success, warning, danger, radius, font; window.mindroom.colorScheme (light|dark).

        Submit & state: nothing reaches you until commit via window.mindroom.submit(data, {label: "short summary"}) (<512KB JSON; decimals as text) or <form data-mindroom-label="...">. User confirms before Canvas response (<canvas_event_id>, revision <event_id>): <label> + JSON (older = earlier) or Canvas error (<canvas_event_id>, revision <event_id>): + lines.
        Auto-persist inputs/selects/textareas via stable id/name (not type=password/hidden/autocomplete="off"; rename id if meaning changes; redraw on restored input). Other state: call window.mindroom.saveState(v) on change (<=256K chars JSON; larger throws); restore from window.mindroom.state on load (undefined if unset; read defensively). Device-local (100 recent) unless share_state=true (at creation; user notified; room-readable via read_canvas_state without submit; no secrets).

        path reads immediately: call ONLY after file writes finish, never in parallel. Success = request sent, not opened/answered.

        Args:
            title: Required on create (1 line <=120 chars); omit on update to keep original.
            html: Inline HTML; pass html or path, never both (one required on update).
            path: Workspace HTML path instead of html; finish writes first (one required on update).
            canvas_event_id: Original $ID in this conversation to replace in place; still needs html or path, cannot reopen unchanged.
            share_state: Expose state to read_canvas_state (on create; kept on update).
            inline: Show inline vs side panel (on create; kept on update).
            raw: Omit Chat styles/vars (per revision; re-pass on update).
            width: Target width px, 320-2560 (per revision; panel uses own).
            libraries: Bundled libs (re-pass on update).

        """
        validated = self._validated_context("show_canvas")
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        # Models often send an empty string for an omitted optional argument.
        canvas_event_id = canvas_event_id or None
        title = (title or "").strip()
        input_error = self._canvas_input_error(title, html, path, canvas_event_id, width, libraries)
        page = input_error or await self._read_canvas_page(html, path)
        if isinstance(page, str):
            return page
        target = await self._canvas_metadata(context, requester_id, canvas_event_id, title, share_state=share_state)
        if isinstance(target, str):
            return target
        metadata, title = target
        body = f"Interactive panel: {title}. Open it in MindRoom Chat to respond."
        canvas = await self._canvas_field(
            context,
            metadata,
            body,
            title,
            page,
            canvas_event_id,
            raw=raw,
            width=width,
            libraries=libraries,
        )
        if isinstance(canvas, str):
            return canvas
        if canvas_event_id is None:
            return await self._send_validated_action(
                context,
                requester_id,
                "show_canvas",
                body,
                {
                    "canvas": canvas,
                    **({"share_state": True} if share_state else {}),
                    **({"inline": True} if inline else {}),
                },
                formatted_body=html_lib.escape(body),
            )
        return await self._update_canvas(context, canvas_event_id, body, {**metadata, "canvas": canvas})

    async def read_canvas_state(self, canvas_event_id: str) -> str:
        """Read latest state from a share_state=true canvas: saveState, kept inputs (#id/name), timestamp. Empty until user changes something; ok anytime (incl. scheduled tasks) without needing submit.

        Args:
            canvas_event_id: Event ID ($...) of a share_state=true canvas in this conversation.

        """
        action = "read_canvas_state"
        validated = self._validated_context(action)
        if isinstance(validated, str):
            return validated
        context, requester_id = validated
        if not canvas_event_id.startswith("$"):
            return self._payload(
                "error",
                action=action,
                message=_CANVAS_EVENT_ID_ERROR,
            )
        original = await self._canvas_target(context, requester_id, canvas_event_id, action=action)
        if isinstance(original, str):
            return original
        if original.get("share_state") is not True:
            return self._payload(
                "error",
                action=action,
                canvas_event_id=canvas_event_id,
                message="This canvas does not share its state; show a new canvas with share_state=True.",
            )
        shared = await _latest_shared_canvas_state(context.client, context.room_id, canvas_event_id, requester_id)
        if not isinstance(shared, tuple):
            # No copy is a plain answer; an unreadable newest copy is an error, never an older copy.
            status, message = (
                ("error", shared)
                if shared
                else ("ok", "Nothing shared yet: the user has not changed anything in this canvas.")
            )
            return self._payload(status, action=action, canvas_event_id=canvas_event_id, message=message)
        state, inputs, shared_at = shared
        return self._payload(
            "ok",
            action=action,
            canvas_event_id=canvas_event_id,
            shared_at=shared_at,
            state=state,
            inputs=inputs,
        )

    @classmethod
    async def _canvas_metadata(
        cls,
        context: ToolRuntimeContext,
        requester_id: str,
        canvas_event_id: str | None,
        title: str,
        *,
        share_state: bool = False,
    ) -> tuple[dict[str, object], str] | str:
        """Return the request metadata and the title; an update without one keeps the canvas's first title."""
        metadata = cls._action_metadata(context, requester_id, "show_canvas", {})
        if canvas_event_id is None:
            return metadata, title
        original = await cls._canvas_target(context, requester_id, canvas_event_id)
        if isinstance(original, str):
            return original
        # Chat accepts an edit only when its authority fields equal the original request's, and
        # whether a canvas shares its state is one of them, so the user is told before anything is shared.
        metadata["thread_id"] = original.get("thread_id")
        if original.get("inline") is True:
            metadata["inline"] = True
        if original.get("share_state") is True:
            metadata["share_state"] = True
        elif share_state:
            return cls._canvas_error(
                "Sharing is decided when a canvas is first shown; show a new canvas with share_state=True.",
                canvas_event_id=canvas_event_id,
            )
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
        html: str | None,
        path: str | None,
        canvas_event_id: str | None,
        width: int | None,
        libraries: list[_CanvasLibrary] | None,
    ) -> str | None:
        if presentation_error := _canvas_presentation_error(width, libraries):
            return cls._canvas_error(presentation_error)
        if canvas_event_id is not None and not canvas_event_id.startswith("$"):
            return cls._canvas_error(_CANVAS_EVENT_ID_ERROR, canvas_event_id=canvas_event_id)
        # An update may omit the title; it is checked once the canvas's own title is known.
        if (title or canvas_event_id is None) and not _canvas_title_is_valid(title):
            return cls._canvas_error(_CANVAS_TITLE_ERROR)
        if bool(html) == bool(path):
            message = (
                "Give exactly one of html or path. canvas_event_id replaces content in place and cannot reopen an "
                "unchanged canvas; pass html or path to update it, or tell the user to select it in the room header."
                if canvas_event_id is not None and not html and not path
                else "Give exactly one of html or path."
            )
            return cls._canvas_error(message)
        if html and not html.strip():
            return cls._canvas_error("Canvas HTML must not be empty.")
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
        *,
        raw: bool = False,
        width: int | None = None,
        libraries: list[_CanvasLibrary] | None = None,
    ) -> dict[str, object] | str:
        """Carry a page inside the event when an edit of it fits, otherwise as uploaded media."""
        presentation: dict[str, object] = {}
        if raw:
            presentation["raw"] = True
        if width is not None:
            presentation["width"] = width
        if libraries:
            presentation["libraries"] = list(dict.fromkeys(libraries))
        # Serialized JSON is never smaller than the UTF-8 page, so larger pages skip the measurement.
        if len(page) < EDIT_MESSAGE_SIZE_LIMIT:
            inline: dict[str, object] = {"title": title, "html": page.decode("utf-8"), **presentation}
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
        return {"title": title, "document": document, **presentation}

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
        *,
        action: str = "show_canvas",
    ) -> dict[str, object] | str:
        """Return the original request of this agent's own canvas for the same requester and room."""
        update = action == "show_canvas"
        instead = "; call show_canvas without canvas_event_id instead." if update else "."

        def error(message: str) -> str:
            return cls._payload("error", action=action, message=message, canvas_event_id=canvas_event_id)

        response = await context.client.room_get_event(context.room_id, canvas_event_id)
        if not isinstance(response, nio.RoomGetEventResponse) or isinstance(response.event, nio.MegolmEvent):
            return error(f"The canvas {'to update ' if update else ''}could not be read in this room.")
        source = response.event.source if isinstance(response.event.source, dict) else {}
        unsigned = source.get("unsigned")
        if isinstance(unsigned, dict) and "redacted_because" in unsigned:
            return error(f"That canvas was deleted{instead}")
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
            return error(f"Only your own canvases can be {'updated' if update else 'read'}{instead}")
        # A room-level canvas is answered by a reply that starts a thread, so any thread of the room may update it.
        thread_id = metadata.get("thread_id")
        if (
            metadata.get("requester_id") != requester_id
            or metadata.get("room_id") != context.room_id
            or (thread_id is not None and thread_id != context.resolved_thread_id)
        ):
            return error(
                "That canvas belongs to another conversation"
                + ("; show a new canvas here instead." if update else "."),
            )
        return metadata


async def _send_computer_notice(context: ToolRuntimeContext, requester_id: str, notice: _ComputerNotice) -> str:
    """Send the show_computer notice; a delivered one counts for this conversation and reply."""
    result = await ChatUITools._send_validated_action(context, requester_id, "show_computer", _SHOW_COMPUTER_BODY, {})
    if json.loads(result)["status"] == "ok":
        notice.delivered, notice.reply = True, context.correlation_id
    return result


async def show_computer_once() -> None:
    """Send the show_computer notice unless this agent already showed the requester its computer here.

    Contexts where Chat UI actions are unsupported, such as teams, the router, or a missing
    runtime context, send nothing.
    """
    validated = ChatUITools._validated_context("show_computer")
    if isinstance(validated, str):
        return
    context, requester_id = validated
    notice = _computer_notice(context, requester_id)
    # Sends for one conversation take turns, so concurrent first calls send one notice. An
    # undelivered or cancelled notice does not count, so the next browser call tries again.
    async with notice.lock:
        if notice.delivered:
            return
        payload = json.loads(await _send_computer_notice(context, requester_id, notice))
        if payload["status"] == "ok":
            return
        logger.warning(
            "The worker computer notice was not delivered",
            reason=payload.get("message"),
            room_id=context.room_id,
            thread_id=context.resolved_thread_id,
        )


def _parsed_json(value: object) -> object:
    """A JSON text Chat shared, parsed; anything else is left out."""
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


async def _latest_shared_canvas_state(
    client: nio.AsyncClient,
    room_id: str,
    canvas_event_id: str,
    requester_id: str,
) -> tuple[object, object, str] | str | None:
    """Return the page state, kept inputs, and time of the newest copy the requester's Chat shared.

    The newest copy still in the room is the answer: when it cannot be read, the reason is returned
    instead of an older copy, so the agent never takes superseded choices for current ones. None means
    no copy.
    """
    relations = client.room_get_event_relations(
        room_id,
        canvas_event_id,
        RelationshipType.reference,
        direction=nio.MessageDirection.back,
    )
    async with contextlib.aclosing(relations):
        scanned = 0
        async for related in relations:
            scanned += 1
            if scanned > _CANVAS_STATE_SCAN_LIMIT:
                return "The user's copy is not among the newest references to this canvas; try again later."
            if related.sender != requester_id:
                continue
            event = related
            if isinstance(event, nio.MegolmEvent):
                # Encrypted rooms hide the event type, so it is checked after decrypting.
                try:
                    event = client.decrypt_event(event) if client.olm is not None else None
                except nio.EncryptionError:
                    event = None
                if event is None:
                    return "The newest copy could not be decrypted yet; try again later."
            if isinstance(event, nio.RedactedEvent):
                # A deleted copy is gone; servers drop it from the relations anyway, so the next one counts.
                continue
            source = event.source if isinstance(event.source, dict) else {}
            if (
                getattr(event, "type", None) != _CANVAS_STATE_EVENT_TYPE
                and source.get("type") != _CANVAS_STATE_EVENT_TYPE
            ):
                continue
            content = (await resolve_event_source_content(source, client)).get("content")
            if not isinstance(content, dict) or content.get("version") != 1:
                return "The newest copy could not be read; try again later."
            shared_at = datetime.fromtimestamp(related.server_timestamp / 1000, tz=UTC).isoformat()
            return _parsed_json(content.get("json")), _parsed_json(content.get("inputs")), shared_at
    return None
