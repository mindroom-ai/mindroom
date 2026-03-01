"""OpenClaw-compatible toolkit surface for incremental parity work."""

from __future__ import annotations

import inspect
import json
import os
import shlex
import subprocess
from threading import Lock
from typing import Any

import nio
from agno.tools import Toolkit
from agno.tools.duckduckgo import DuckDuckGoTools
from agno.tools.website import WebsiteTools

from mindroom.custom_tools import subagents as _subagents_mod
from mindroom.custom_tools.coding import CodingTools
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.custom_tools.subagents import SubAgentsTools
from mindroom.logging_config import get_logger
from mindroom.matrix.client import fetch_thread_history
from mindroom.matrix.message_content import extract_and_resolve_message
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context
from mindroom.tools_metadata import get_tool_by_name

logger = get_logger(__name__)


class OpenClawCompatTools(Toolkit):
    """OpenClaw-style tool names exposed as a single toolkit."""

    _shell_path_lock: Lock = Lock()
    _login_shell_path: str | None = None
    _login_shell_path_loaded = False
    _login_shell_path_applied = False
    _LOGIN_SHELL_TIMEOUT_SECONDS = 15
    _CODING_ERROR_PREFIXES = (
        "Error:",
        "Error reading file:",
        "Error writing file:",
        "Error listing directory:",
        "Error running grep",
    )

    def __init__(self) -> None:
        """Initialize the OpenClaw compatibility toolkit."""
        self._subagents_tools = SubAgentsTools()
        super().__init__(
            name="openclaw_compat",
            tools=[
                self._subagents_tools.agents_list,
                self._subagents_tools.sessions_send,
                self._subagents_tools.sessions_spawn,
                self._subagents_tools.list_sessions,
                self.message,
                self.cron,
                self.web_search,
                self.web_fetch,
                self.browser,
                self.exec,
                self.process,
                self.read_file,
                self.edit_file,
                self.write_file,
                self.grep,
                self.find_files,
                self.ls,
            ],
        )
        self._scheduler = SchedulerTools()
        self._duckduckgo = DuckDuckGoTools()
        self._website = WebsiteTools()
        self._shell = get_tool_by_name("shell")
        self._browser_tool: Toolkit | None = None
        self._coding = CodingTools()

    @staticmethod
    def _payload(tool_name: str, status: str, **kwargs: object) -> str:
        """Return a structured JSON payload."""
        payload: dict[str, object] = {
            "status": status,
            "tool": tool_name,
        }
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _context_error(tool_name: str) -> str:
        """Return a structured context error payload."""
        return OpenClawCompatTools._payload(
            tool_name,
            "error",
            message="OpenClaw tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _coding_status(cls, result: str) -> str:
        """Map CodingTools string results to stable status values."""
        return "error" if result.startswith(cls._CODING_ERROR_PREFIXES) else "ok"

    @staticmethod
    def _merge_paths(existing_path: str, shell_path: str) -> str:
        """Prepend login-shell PATH entries while keeping order and deduplicating."""
        merged_parts: list[str] = []
        seen: set[str] = set()
        for part in [*shell_path.split(os.pathsep), *existing_path.split(os.pathsep)]:
            normalized = part.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged_parts.append(normalized)
        return os.pathsep.join(merged_parts)

    @classmethod
    def _read_login_shell_path(cls) -> str | None:
        """Read PATH from the user's login shell."""
        if os.name == "nt":
            return None

        shell = os.environ.get("SHELL", "").strip() or "/bin/sh"
        try:
            result = subprocess.run(
                [shell, "-l", "-c", "env -0"],
                capture_output=True,
                check=False,
                timeout=cls._LOGIN_SHELL_TIMEOUT_SECONDS,
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug(f"Login shell PATH probe failed: {exc}")
            return None

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="ignore").strip()
            message = f"Login shell PATH probe exited with {result.returncode}"
            if stderr:
                message = f"{message}: {stderr}"
            logger.debug(message)
            return None

        for env_entry in result.stdout.decode("utf-8", errors="ignore").split("\0"):
            key, sep, value = env_entry.partition("=")
            if sep and key == "PATH":
                resolved_path = value.strip()
                return resolved_path or None
        return None

    @classmethod
    def _ensure_login_shell_path(cls) -> None:
        """Apply login-shell PATH to this process once for OpenClaw shell aliases."""
        if os.name == "nt":
            return

        with cls._shell_path_lock:
            if cls._login_shell_path_applied:
                return

            if not cls._login_shell_path_loaded:
                shell_path = cls._read_login_shell_path()
                if not shell_path:
                    return
                cls._login_shell_path = shell_path
                cls._login_shell_path_loaded = True

            shell_path = cls._login_shell_path
            if not shell_path:
                cls._login_shell_path_loaded = False
                return

            merged = cls._merge_paths(os.environ.get("PATH", ""), shell_path)
            if merged:
                os.environ["PATH"] = merged
            cls._login_shell_path_applied = True

    async def _message_send_or_reply(
        self,
        context: OpenClawToolContext,
        *,
        action: str,
        message: str | None,
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        if message is None or not message.strip():
            return self._payload("message", "error", action=action, message="Message cannot be empty.")
        if action in {"thread-reply", "reply"} and effective_thread_id is None:
            return self._payload("message", "error", action=action, message="thread_id is required for replies.")

        event_id = await _subagents_mod.send_matrix_text(
            context,
            room_id=room_id,
            text=message.strip(),
            thread_id=effective_thread_id,
        )
        if event_id is None:
            return self._payload(
                "message",
                "error",
                action=action,
                room_id=room_id,
                message="Failed to send message to Matrix.",
            )
        return self._payload(
            "message",
            "ok",
            action=action,
            room_id=room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
        )

    async def _message_react(
        self,
        context: OpenClawToolContext,
        *,
        message: str | None,
        room_id: str,
        target: str | None,
    ) -> str:
        if target is None:
            return self._payload("message", "error", action="react", message="target event_id is required.")

        reaction = message.strip() if message and message.strip() else "👍"
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target,
                "key": reaction,
            },
        }
        response = await context.client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=content,
        )
        if isinstance(response, nio.RoomSendResponse):
            return self._payload(
                "message",
                "ok",
                action="react",
                room_id=room_id,
                target=target,
                reaction=reaction,
                event_id=response.event_id,
            )
        return self._payload(
            "message",
            "error",
            action="react",
            room_id=room_id,
            target=target,
            reaction=reaction,
            response=str(response),
        )

    async def _message_read(
        self,
        context: OpenClawToolContext,
        *,
        room_id: str,
        effective_thread_id: str | None,
    ) -> str:
        read_limit = 20
        if effective_thread_id is not None:
            thread_messages = await fetch_thread_history(context.client, room_id, effective_thread_id)
            return self._payload(
                "message",
                "ok",
                action="read",
                room_id=room_id,
                thread_id=effective_thread_id,
                messages=thread_messages[-read_limit:],
            )

        response = await context.client.room_messages(
            room_id,
            limit=read_limit,
            direction=nio.MessageDirection.back,
            message_filter={"types": ["m.room.message"]},
        )
        if not isinstance(response, nio.RoomMessagesResponse):
            return self._payload(
                "message",
                "error",
                action="read",
                room_id=room_id,
                response=str(response),
            )

        resolved = [
            await extract_and_resolve_message(event, context.client)
            for event in reversed(response.chunk)
            if isinstance(event, nio.RoomMessageText)
        ]
        return self._payload(
            "message",
            "ok",
            action="read",
            room_id=room_id,
            messages=resolved,
        )

    async def message(
        self,
        action: str = "send",
        message: str | None = None,
        channel: str | None = None,
        target: str | None = None,
        thread_id: str | None = None,
    ) -> str:
        """Send or manage cross-channel messages."""
        context = get_openclaw_tool_context()
        if context is None:
            return self._context_error("message")

        normalized_action = action.strip().lower()
        room_id = channel or context.room_id

        if normalized_action in {"send", "thread-reply", "reply"}:
            effective_thread_id = thread_id
            if normalized_action in {"thread-reply", "reply"} and effective_thread_id is None:
                effective_thread_id = context.thread_id
            return await self._message_send_or_reply(
                context,
                action=normalized_action,
                message=message,
                room_id=room_id,
                effective_thread_id=effective_thread_id,
            )
        if normalized_action == "react":
            return await self._message_react(
                context,
                message=message,
                room_id=room_id,
                target=target,
            )
        if normalized_action == "read":
            return await self._message_read(
                context,
                room_id=room_id,
                effective_thread_id=thread_id or context.thread_id,
            )

        return self._payload(
            "message",
            "error",
            action=action,
            message="Unsupported action. Use send, thread-reply, react, or read.",
        )

    async def cron(self, request: str) -> str:
        """Schedule a task using the scheduler tool."""
        if not request.strip():
            return self._payload("cron", "error", message="request cannot be empty")
        result = await self._scheduler.schedule(request)
        return self._payload("cron", "ok", result=result)

    async def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web via DuckDuckGo alias."""
        if not query.strip():
            return self._payload("web_search", "error", message="query cannot be empty")
        result = self._duckduckgo.web_search(query=query, max_results=max_results)
        return self._payload("web_search", "ok", result=result)

    async def web_fetch(self, url: str) -> str:
        """Fetch web content via website tool alias."""
        if not url.strip():
            return self._payload("web_fetch", "error", message="url cannot be empty")
        result = self._website.read_url(url.strip())
        return self._payload("web_fetch", "ok", result=result)

    def _get_browser_tool(self) -> Toolkit:
        if self._browser_tool is None:
            self._browser_tool = get_tool_by_name("browser")
        return self._browser_tool

    async def browser(
        self,
        action: str,
        target: str | None = None,
        node: str | None = None,
        profile: str | None = None,
        target_url: str | None = None,
        target_id: str | None = None,
        limit: int | None = None,
        max_chars: int | None = None,
        mode: str | None = None,
        snapshot_format: str | None = None,
        refs: str | None = None,
        interactive: bool | None = None,
        compact: bool | None = None,
        depth: int | None = None,
        selector: str | None = None,
        frame: str | None = None,
        labels: bool | None = None,
        full_page: bool | None = None,
        ref: str | None = None,
        element: str | None = None,
        type_: str | None = None,
        level: str | None = None,
        paths: list[str] | None = None,
        input_ref: str | None = None,
        timeout_ms: int | None = None,
        accept: bool | None = None,
        prompt_text: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> str:
        """Invoke the first-class browser tool via OpenClaw-compatible shape."""
        try:
            browser_tool = self._get_browser_tool()
        except ImportError as exc:
            return self._payload("browser", "error", message=f"browser tool unavailable: {exc}")

        browser_function = browser_tool.functions.get("browser") or browser_tool.async_functions.get("browser")
        if browser_function is None or browser_function.entrypoint is None:
            return self._payload("browser", "error", message="browser tool does not expose browser entrypoint.")

        call_kwargs: dict[str, Any] = {
            "action": action,
            "target": target,
            "node": node,
            "profile": profile,
            "targetUrl": target_url,
            "targetId": target_id,
            "limit": limit,
            "maxChars": max_chars,
            "mode": mode,
            "snapshotFormat": snapshot_format,
            "refs": refs,
            "interactive": interactive,
            "compact": compact,
            "depth": depth,
            "selector": selector,
            "frame": frame,
            "labels": labels,
            "fullPage": full_page,
            "ref": ref,
            "element": element,
            "type": type_,
            "level": level,
            "paths": paths,
            "inputRef": input_ref,
            "timeoutMs": timeout_ms,
            "accept": accept,
            "promptText": prompt_text,
            "request": request,
        }
        call_kwargs = {key: value for key, value in call_kwargs.items() if value is not None}

        try:
            result = browser_function.entrypoint(**call_kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return self._payload("browser", "error", action=action, message=str(exc))

        return self._payload("browser", "ok", action=action, result=result)

    async def _run_shell(self, command: str, tool_name: str) -> str:
        """Shared shell execution for exec and process."""
        if not command.strip():
            return self._payload(tool_name, "error", message="command cannot be empty")

        parse_error: str | None = None
        try:
            args = shlex.split(command)
        except ValueError as exc:
            parse_error = f"invalid shell command: {exc}"
            args = []

        if parse_error is not None or not args:
            return self._payload(
                tool_name,
                "error",
                command=command,
                message=parse_error or "command parsed to empty args",
            )

        shell_function = self._shell.functions.get("run_shell_command") or self._shell.async_functions.get(
            "run_shell_command",
        )
        if shell_function is None or shell_function.entrypoint is None:
            return self._payload(
                tool_name,
                "error",
                message="shell tool does not expose run_shell_command.",
            )

        # Agno ShellTools doesn't accept per-call env overrides, so for OpenClaw
        # compatibility we intentionally enrich process-wide PATH once before exec aliases.
        # Subsequent subprocess calls in this process observe the merged PATH.
        self._ensure_login_shell_path()

        try:
            result = shell_function.entrypoint(args)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.exception(f"Shell command failed: {command}")
            return self._payload(tool_name, "error", command=command, message="shell command failed")

        return self._payload(tool_name, "ok", command=command, result=result)

    async def exec(self, command: str) -> str:
        """Execute a shell command via shell tool alias."""
        return await self._run_shell(command, "exec")

    async def process(self, command: str) -> str:
        """Execute a shell command (alias for exec)."""
        return await self._run_shell(command, "process")

    # ── Coding tool aliases ────────────────────────────────────────────

    def read_file(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Read a file with line numbers and pagination hints."""
        result = self._coding.read_file(path, offset, limit)
        status = self._coding_status(result)
        return self._payload("read_file", status, result=result)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace a specific text occurrence in a file using fuzzy matching."""
        result = self._coding.edit_file(path, old_text, new_text)
        status = self._coding_status(result)
        return self._payload("edit_file", status, result=result)

    def write_file(self, path: str, content: str) -> str:
        """Write content to a file, creating parent directories if needed."""
        result = self._coding.write_file(path, content)
        status = self._coding_status(result)
        return self._payload("write_file", status, result=result)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int = 100,
    ) -> str:
        """Search file contents for a pattern."""
        result = self._coding.grep(pattern, path, glob, ignore_case, literal, context, limit)
        status = self._coding_status(result)
        return self._payload("grep", status, result=result)

    def find_files(
        self,
        pattern: str,
        path: str | None = None,
        limit: int = 1000,
    ) -> str:
        """Find files matching a glob pattern."""
        result = self._coding.find_files(pattern, path, limit)
        status = self._coding_status(result)
        return self._payload("find_files", status, result=result)

    def ls(self, path: str | None = None, limit: int = 500) -> str:
        """List directory contents."""
        result = self._coding.ls(path, limit)
        status = self._coding_status(result)
        return self._payload("ls", status, result=result)
