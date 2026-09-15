"""Playwright MCP extension adapter for the Matrix desktop bridge."""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.types import ImageContent, TextContent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp.types import CallToolResult

PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.78"
_MAX_RESULT_CHARS = 32_000
_MAX_RESULT_JSON_BYTES = 24_000
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_TRUNCATION_SUFFIX = "\n…"
_OBSERVE_ACTIONS = frozenset({"status", "profiles", "tabs", "snapshot", "screenshot", "console"})
_CONTROL_ACTIONS = frozenset({"start", "stop", "open", "focus", "close", "navigate", "pdf", "upload", "dialog", "act"})
_SUPPORTED_CONTROL_ACTIONS = ("start", "stop", "open")
_UNBOUND_PAGE_ACTIONS = _CONTROL_ACTIONS - frozenset(_SUPPORTED_CONTROL_ACTIONS)
BROWSER_ACTIONS = _OBSERVE_ACTIONS | _CONTROL_ACTIONS


class PlaywrightBrowserError(RuntimeError):
    """The local Playwright MCP browser provider could not complete a request."""


class PlaywrightActionOutcomeUnknownError(PlaywrightBrowserError):
    """A browser control call failed after dispatch and may have changed page state."""


@dataclass(frozen=True, slots=True)
class BrowserImage:
    """One image returned by a browser MCP tool call."""

    content: bytes
    mime_type: str


@dataclass(frozen=True, slots=True)
class BrowserProviderResult:
    """Bounded browser result ready for a Matrix response."""

    payload: dict[str, object]
    image: BrowserImage | None = None


class BrowserProvider(Protocol):
    """Async browser capability hosted beside the local desktop bridge."""

    async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
        """Execute one validated browser action."""
        ...

    async def close(self) -> None:
        """Close local browser-provider resources."""
        ...


@dataclass(slots=True)
class _QueuedCall:
    tool_name: str
    arguments: dict[str, object]
    future: asyncio.Future[CallToolResult]


@dataclass(frozen=True, slots=True)
class _MCPCall:
    tool_name: str
    arguments: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ScreenshotOutput:
    path: Path
    mime_type: str


class PlaywrightMCPBrowserProvider:
    """Drive the user's existing browser profile through Playwright MCP extension mode."""

    def __init__(
        self,
        *,
        output_dir: Path,
        executable_path: Path | None = None,
        user_data_dir: Path | None = None,
        command: str = "npx",
        package: str = PLAYWRIGHT_MCP_PACKAGE,
        call_timeout_seconds: float = 90.0,
        extension_token: str | None = None,
    ) -> None:
        if isinstance(call_timeout_seconds, bool) or not 1 <= call_timeout_seconds <= 120:
            msg = "Playwright MCP call timeout must be between 1 and 120 seconds."
            raise ValueError(msg)
        if not package.strip():
            msg = "Playwright MCP package must not be empty."
            raise ValueError(msg)
        if extension_token is not None and not extension_token.strip():
            msg = "Playwright MCP extension token must not be empty when provided."
            raise ValueError(msg)
        self._output_dir = output_dir.expanduser().resolve()
        self._executable_path = executable_path.expanduser().resolve() if executable_path is not None else None
        self._user_data_dir = user_data_dir.expanduser().resolve() if user_data_dir is not None else None
        self._command = command
        self._package = package
        self._call_timeout_seconds = float(call_timeout_seconds)
        self._extension_token = extension_token
        self._queue: asyncio.Queue[_QueuedCall | None] | None = None
        self._actor_task: asyncio.Task[None] | None = None
        self._actor_lock = asyncio.Lock()
        self._closed = False

    async def execute(self, action: str, parameters: dict[str, object]) -> BrowserProviderResult:
        """Translate the stable MindRoom browser surface into Playwright MCP calls."""
        if action not in BROWSER_ACTIONS:
            msg = f"Unsupported Playwright browser action: {action}."
            raise PlaywrightBrowserError(msg)
        if action == "profiles":
            _reject_unexpected(parameters, frozenset())
            return BrowserProviderResult(
                {
                    **_browser_capabilities(),
                    "action": action,
                    "profiles": ["extension"],
                    "provider": "playwright_mcp_extension",
                    "selected_profile": "extension",
                    "status": "ok",
                },
            )
        if action == "status" and not self.running:
            _reject_unexpected(parameters, frozenset())
            return BrowserProviderResult(
                {
                    **_browser_capabilities(),
                    "action": action,
                    "provider": "playwright_mcp_extension",
                    "running": False,
                    "status": "ok",
                },
            )
        if action == "stop":
            _reject_unexpected(parameters, frozenset())
            await self._stop_actor(permanent=False)
            return BrowserProviderResult(
                {
                    **_browser_capabilities(),
                    "action": action,
                    "provider": "playwright_mcp_extension",
                    "running": False,
                    "status": "ok",
                },
            )

        if not self.running and not browser_action_requires_control(action):
            msg = (
                "Playwright browser observation requires an active local connection. "
                "Run browser(action='start', target='desktop') while the desktop control lease is active."
            )
            raise PlaywrightBrowserError(msg)

        calls, screenshot_output = self._calls_with_screenshot_output(action, parameters)
        try:
            last_result: CallToolResult | None = None
            for call in calls:
                last_result = await self._call_tool(call.tool_name, call.arguments)
                _raise_result_error(action, last_result)
            if last_result is None:
                msg = f"Browser action {action} did not produce an MCP call."
                raise PlaywrightBrowserError(msg)  # noqa: TRY301
            provider_result = _provider_result(action, last_result, max_chars=_result_max_chars(parameters))
            return _provider_result_with_screenshot(provider_result, screenshot_output)
        except PlaywrightBrowserError as exc:
            if browser_action_requires_control(action):
                raise PlaywrightActionOutcomeUnknownError(str(exc)) from exc
            raise
        finally:
            _remove_screenshot_output(screenshot_output)

    @property
    def running(self) -> bool:
        """Return whether the local MCP actor is alive."""
        return self._actor_task is not None and not self._actor_task.done()

    async def close(self) -> None:
        """Permanently close the provider after its already queued calls finish."""
        await self._stop_actor(permanent=True)

    async def _stop_actor(self, *, permanent: bool) -> None:
        """Stop the current actor, optionally rejecting every later MCP call."""
        async with self._actor_lock:
            if permanent:
                self._closed = True
            task = self._actor_task
            queue = self._queue
            self._actor_task = None
            self._queue = None
            if task is None:
                return
            if queue is not None:
                queue.put_nowait(None)
        await task

    async def _call_tool(self, tool_name: str, arguments: dict[str, object]) -> CallToolResult:
        async with self._actor_lock:
            if self._closed:
                msg = "Playwright browser provider is closed."
                raise PlaywrightBrowserError(msg)
            future: asyncio.Future[CallToolResult] = asyncio.get_running_loop().create_future()
            queued_call = _QueuedCall(tool_name=tool_name, arguments=arguments, future=future)
            if self.running:
                assert self._queue is not None
                self._queue.put_nowait(queued_call)
            else:
                self._start_actor(queued_call)
        try:
            async with asyncio.timeout(self._call_timeout_seconds):
                return await future
        except TimeoutError as exc:
            future.cancel()
            msg = f"Playwright MCP tool {tool_name} did not answer within {self._call_timeout_seconds:g} seconds."
            raise PlaywrightBrowserError(msg) from exc

    def _start_actor(self, first_call: _QueuedCall) -> None:
        """Start one MCP actor with work already queued so startup failures reach the caller."""
        if shutil.which(self._command) is None:
            msg = f"Playwright browser support requires '{self._command}' on the local computer."
            raise PlaywrightBrowserError(msg)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.chmod(0o700)
        queue: asyncio.Queue[_QueuedCall | None] = asyncio.Queue()
        queue.put_nowait(first_call)
        self._queue = queue
        self._actor_task = asyncio.create_task(self._run_actor(queue), name="playwright_mcp_extension")

    def _remove_cancelled_call_screenshot(self, call: _QueuedCall) -> None:
        """Remove a screenshot only after its timed-out MCP call has actually returned."""
        if call.tool_name != "browser_take_screenshot":
            return
        filename = call.arguments.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return
        path = (self._output_dir / filename).resolve()
        if path.parent == self._output_dir:
            path.unlink(missing_ok=True)

    async def _run_actor(self, queue: asyncio.Queue[_QueuedCall | None]) -> None:  # noqa: C901, PLR0912
        active: _QueuedCall | None = None
        try:
            parameters = StdioServerParameters(
                command=self._command,
                args=self._server_args(),
                env=self._server_environment(),
                cwd=str(self._output_dir),
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=self._call_timeout_seconds),
                ) as session,
            ):
                await session.initialize()
                while True:
                    active = await queue.get()
                    if active is None:
                        return
                    if active.future.done():
                        active = None
                        continue
                    try:
                        result = await session.call_tool(
                            active.tool_name,
                            active.arguments,
                            read_timeout_seconds=timedelta(seconds=self._call_timeout_seconds),
                        )
                        if not active.future.done():
                            active.future.set_result(result)
                    except Exception as exc:
                        if not active.future.done():
                            active.future.set_exception(_browser_error(active.tool_name, exc))
                    finally:
                        if active.future.cancelled():
                            self._remove_cancelled_call_screenshot(active)
                        active = None
        except Exception as exc:
            error = _browser_error(active.tool_name if active is not None else "startup", exc)
            if active is not None and not active.future.done():
                active.future.set_exception(error)
            while not queue.empty():
                queued = queue.get_nowait()
                if queued is not None and not queued.future.done():
                    queued.future.set_exception(error)
        finally:
            if self._actor_task is asyncio.current_task():
                self._actor_task = None
                self._queue = None

    def _server_args(self) -> list[str]:
        args = [
            "--yes",
            self._package,
            "--extension",
            "--caps",
            "vision",
            "--output-dir",
            str(self._output_dir),
            "--output-mode",
            "stdout",
        ]
        if self._executable_path is not None:
            args.extend(["--executable-path", str(self._executable_path)])
        return args

    def _server_environment(self) -> dict[str, str]:
        environment = get_default_environment()
        if self._user_data_dir is not None:
            environment["PWTEST_EXTENSION_USER_DATA_DIR"] = str(self._user_data_dir)
        if self._extension_token is not None:
            environment["PLAYWRIGHT_MCP_EXTENSION_TOKEN"] = self._extension_token
        return environment

    def _calls_with_screenshot_output(
        self,
        action: str,
        parameters: dict[str, object],
    ) -> tuple[list[_MCPCall], _ScreenshotOutput | None]:
        calls = _mcp_calls(action, parameters)
        if action != "screenshot":
            return calls, None
        image_type = _optional_str(parameters, "type") or "png"
        path = self._output_dir / f"mindroom-screenshot-{uuid4().hex}.{image_type}"
        screenshot_call = calls[-1]
        calls[-1] = _MCPCall(
            screenshot_call.tool_name,
            {**screenshot_call.arguments, "filename": path.name},
        )
        return calls, _ScreenshotOutput(path=path, mime_type=f"image/{image_type}")


def browser_action_requires_control(action: str) -> bool:
    """Return whether one browser action mutates browser or page state."""
    if action not in BROWSER_ACTIONS:
        msg = f"Unsupported Playwright browser action: {action}."
        raise PlaywrightBrowserError(msg)
    return action in _CONTROL_ACTIONS


def _mcp_calls(  # noqa: C901
    action: str,
    parameters: dict[str, object],
) -> list[_MCPCall]:
    if parameters.get("targetId") is not None:
        msg = (
            "Playwright extension targetId is unsupported because MCP tab indices can change; "
            "stable page identity is unavailable. Only observation, start, stop, and open are supported."
        )
        raise PlaywrightBrowserError(msg)
    if action in _UNBOUND_PAGE_ACTIONS:
        msg = (
            f"Playwright extension cannot execute {action}: stable page identity is unavailable. "
            "Existing-page control requires upstream stable targeting; reconnecting, reselecting, "
            "or refreshing a snapshot cannot make it safe. Use the host browser for page control, "
            "or select this browser through local desktop controls. "
            "Supported extension control actions: start, stop, open."
        )
        raise PlaywrightBrowserError(msg)
    if action in {"start", "status", "tabs"}:
        _reject_unexpected(parameters, frozenset())
        return [_MCPCall("browser_tabs", {"action": "list"})]
    if action == "open":
        _reject_unexpected(parameters, frozenset({"targetUrl"}))
        return [_MCPCall("browser_tabs", {"action": "new", "url": _required_str(parameters, "targetUrl")})]
    if action == "snapshot":
        allowed = frozenset({"selector", "depth", "maxChars"})
        _reject_unexpected(parameters, allowed)
        arguments: dict[str, object] = {}
        selector = _optional_str(parameters, "selector")
        if selector is not None:
            arguments["target"] = selector
        depth = _optional_positive_int(parameters, "depth")
        if depth is not None:
            arguments["depth"] = depth
        return [_MCPCall("browser_snapshot", arguments)]
    if action == "screenshot":
        allowed = frozenset({"fullPage", "ref", "element", "type"})
        _reject_unexpected(parameters, allowed)
        target = _optional_str(parameters, "ref") or _optional_str(parameters, "element")
        arguments: dict[str, object] = {
            "type": _optional_str(parameters, "type") or "png",
            "scale": "css",
            "fullPage": bool(parameters.get("fullPage", False)),
        }
        if target is not None:
            arguments.update({"element": target, "target": target})
        return [_MCPCall("browser_take_screenshot", arguments)]
    if action == "console":
        _reject_unexpected(parameters, frozenset({"level"}))
        level = _optional_str(parameters, "level") or "info"
        return [_MCPCall("browser_console_messages", {"level": level})]
    msg = f"Unsupported Playwright browser action: {action}."
    raise PlaywrightBrowserError(msg)


def _browser_capabilities() -> dict[str, object]:
    """Report the extension's supported control contract without inventing target IDs."""
    return {
        "stable_targeting": False,
        "supported_control_actions": list(_SUPPORTED_CONTROL_ACTIONS),
    }


def _provider_result(action: str, result: CallToolResult, *, max_chars: int) -> BrowserProviderResult:
    _raise_result_error(action, result)
    text = _truncate_result_text(_result_text(result), max_chars=max_chars)
    images = [block for block in result.content if isinstance(block, ImageContent)]
    image = _browser_image(images[0]) if images else None
    return BrowserProviderResult(
        payload={
            **_browser_capabilities(),
            "action": action,
            "provider": "playwright_mcp_extension",
            "result": text or "Playwright browser action completed.",
            "running": True,
            "status": "ok",
        },
        image=image,
    )


def _result_text(result: CallToolResult) -> str:
    text_parts = [block.text for block in result.content if isinstance(block, TextContent)]
    if result.structuredContent is not None:
        text_parts.append(json.dumps(result.structuredContent, sort_keys=True, ensure_ascii=False))
    return "\n\n".join(part for part in text_parts if part).strip()


def _raise_result_error(action: str, result: CallToolResult) -> None:
    if result.isError:
        text = _truncate_result_text(_result_text(result), max_chars=_MAX_RESULT_CHARS)
        raise PlaywrightBrowserError(text or f"Playwright MCP action {action} failed.")


def _browser_image(block: ImageContent) -> BrowserImage:
    try:
        content = base64.b64decode(block.data, validate=True)
    except ValueError as exc:
        msg = "Playwright MCP returned invalid base64 image data."
        raise PlaywrightBrowserError(msg) from exc
    return _validated_browser_image(content, block.mimeType)


def _browser_image_file(path: Path, mime_type: str) -> BrowserImage:
    if not path.is_file():
        msg = "Playwright MCP did not create the requested screenshot file."
        raise PlaywrightBrowserError(msg)
    with path.open("rb") as image_file:
        content = image_file.read(_MAX_IMAGE_BYTES + 1)
    return _validated_browser_image(content, mime_type)


def _provider_result_with_screenshot(
    provider_result: BrowserProviderResult,
    screenshot_output: _ScreenshotOutput | None,
) -> BrowserProviderResult:
    if screenshot_output is None:
        return provider_result
    return BrowserProviderResult(
        payload=provider_result.payload,
        image=_browser_image_file(screenshot_output.path, screenshot_output.mime_type),
    )


def _remove_screenshot_output(screenshot_output: _ScreenshotOutput | None) -> None:
    if screenshot_output is None:
        return
    screenshot_output.path.unlink(missing_ok=True)


def _validated_browser_image(content: bytes, mime_type: str) -> BrowserImage:
    if mime_type not in {"image/jpeg", "image/png"}:
        msg = f"Playwright MCP returned unsupported image type: {mime_type}."
        raise PlaywrightBrowserError(msg)
    if not content or len(content) > _MAX_IMAGE_BYTES:
        msg = f"Playwright MCP image must contain between 1 and {_MAX_IMAGE_BYTES} bytes."
        raise PlaywrightBrowserError(msg)
    return BrowserImage(content=content, mime_type=mime_type)


def _result_max_chars(parameters: Mapping[str, object]) -> int:
    value = parameters.get("maxChars")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, _MAX_RESULT_CHARS)
    return _MAX_RESULT_CHARS


def _reject_unexpected(parameters: Mapping[str, object], allowed: frozenset[str]) -> None:
    unexpected = sorted(set(parameters) - allowed)
    if unexpected:
        msg = f"Unexpected browser parameters: {', '.join(unexpected)}."
        raise PlaywrightBrowserError(msg)


def _required_str(parameters: Mapping[str, object], key: str) -> str:
    value = _optional_str(parameters, key)
    if value is None:
        msg = f"Browser parameter {key} must be a non-empty string."
        raise PlaywrightBrowserError(msg)
    return value


def _optional_str(parameters: Mapping[str, object], key: str) -> str | None:
    value = parameters.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 8_000:
        msg = f"Browser parameter {key} must be a non-empty string of at most 8000 characters."
        raise PlaywrightBrowserError(msg)
    return value.strip()


def _optional_positive_int(parameters: Mapping[str, object], key: str) -> int | None:
    value = parameters.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"Browser parameter {key} must be a positive integer."
        raise PlaywrightBrowserError(msg)
    return value


def _browser_error(tool_name: str, exc: Exception) -> PlaywrightBrowserError:
    if isinstance(exc, PlaywrightBrowserError):
        return exc
    detail = f"Playwright MCP {tool_name} failed: {exc}"
    return PlaywrightBrowserError(_truncate_result_text(detail, max_chars=_MAX_RESULT_CHARS))


def _truncate_result_text(text: str, *, max_chars: int) -> str:
    """Bound model-visible text by characters and its eventual nio JSON encoding."""
    candidate = text[:max_chars]
    truncated = len(candidate) < len(text)
    rendered = candidate.rstrip() + (_TRUNCATION_SUFFIX if truncated else "")
    if _json_string_size(rendered) <= _MAX_RESULT_JSON_BYTES:
        return rendered

    low = 0
    high = len(candidate)
    while low < high:
        midpoint = (low + high + 1) // 2
        rendered = candidate[:midpoint].rstrip() + _TRUNCATION_SUFFIX
        if _json_string_size(rendered) <= _MAX_RESULT_JSON_BYTES:
            low = midpoint
        else:
            high = midpoint - 1
    return candidate[:low].rstrip() + _TRUNCATION_SUFFIX


def _json_string_size(value: str) -> int:
    """Return the bytes nio's default JSON encoder uses for one string value."""
    return len(json.dumps(value, separators=(",", ":")).encode())


__all__ = [
    "BROWSER_ACTIONS",
    "PLAYWRIGHT_MCP_PACKAGE",
    "BrowserImage",
    "BrowserProvider",
    "BrowserProviderResult",
    "PlaywrightActionOutcomeUnknownError",
    "PlaywrightBrowserError",
    "PlaywrightMCPBrowserProvider",
    "browser_action_requires_control",
]
