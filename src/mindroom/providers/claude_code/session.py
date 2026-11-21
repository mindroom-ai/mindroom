"""Live Claude Code processes of the ``claude_code`` route, and the MCP server that offers them MindRoom's tools.

A process is read by the one request that started or claimed it, or it rests in ``SESSIONS`` under the identity of
the request it answered: parked on tool calls whose results the next request brings, or idle after an answer. Its
tool calls reach an in-process MCP server whose handler only waits for the result MindRoom's own loop produces.
MindRoom's history stays the only record of a conversation: no process is resumed and none saves a transcript.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import mcp.types
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ClaudeSDKError, CLINotFoundError
from mcp.server import Server

from mindroom import __version__
from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.claude_sdk_process import open_client
from mindroom.llm.errors import AuthenticationFailed, ProviderUnavailable
from mindroom.logging_config import get_logger
from mindroom.providers.claude_code.prompt import TOOL_PREFIX, short_name, tool_names
from mindroom.runtime_shutdown import close_at_process_shutdown

if TYPE_CHECKING:
    from collections.abc import Sequence

    from claude_agent_sdk import Message as ClaudeMessage

    from mindroom.llm.provider import ToolSpec
    from mindroom.providers.claude_code.prompt import Emitted
    from mindroom.providers.claude_code.settings import ClaudeCodeSettings

__all__ = ["NOT_SIGNED_IN", "SESSIONS", "LiveSession", "start", "startup_error"]

logger = get_logger(__name__)

# Older binaries ignore verbatim prompts, so a user's ``@path`` or ``/command`` would be read as a Claude Code command.
MIN_VERSION = (2, 1, 248)
# The request metadata Claude Code sends with an MCP tool call: the id of the ``tool_use`` block that made the call.
_TOOL_USE_ID_META = "claudecode/toolUseId"
_SERVER_NAME = "mindroom"
PARKED_TTL_SECONDS = 30 * 60.0
IDLE_TTL_SECONDS = 5 * 60.0
MAX_PROCESSES = 16
_SWEEP_INTERVAL_SECONDS = 60.0
# A tool result that holds images comes back from Claude Code as one message.
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Claude Code cuts a longer MCP tool description itself.
_DESCRIPTION_LIMIT = 2048
# Claude Code features that would duplicate MindRoom's or hide its tools: memory files, compaction, and tool search.
_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # MindRoom already bounds its tool results; Claude Code would replace a longer one with a file it cannot read.
    "MAX_MCP_OUTPUT_TOKENS": "200000",
}
NOT_SIGNED_IN = "Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom."
_NOT_FOUND = (
    "Claude Code was not found. Install it (https://claude.com/claude-code), run `claude auth login` as the user "
    "that runs MindRoom, and set `extra_kwargs.cli_path` if `claude` is not on MindRoom's PATH."
)


@dataclass(eq=False)
class ToolBridge:
    """The tool calls of one Claude Code process: each waits here for the result MindRoom's loop produces.

    ``entered`` holds the tool each call id reached the MCP server with, and ``failure`` says why the process can
    no longer be trusted, such as a call without Claude Code's call id.
    """

    names: dict[str, str]
    messages: asyncio.Queue[ClaudeMessage | Exception] = field(default_factory=asyncio.Queue)
    results: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    entered: dict[str, str] = field(default_factory=dict)
    failure: str | None = None

    def result(self, tool_use_id: str) -> asyncio.Future[dict[str, Any]]:
        """Return the future of one call's result, which the call or its result may reach first."""
        if tool_use_id not in self.results:
            self.results[tool_use_id] = asyncio.get_running_loop().create_future()
        return self.results[tool_use_id]

    def fail(self, reason: str) -> None:
        """Mark the process as broken and wake the request that reads it."""
        self.failure = reason
        self.messages.put_nowait(ClaudeSDKError(reason))

    def server(self, tools: Sequence[ToolSpec]) -> Server:
        """Return the MCP server that offers ``tools`` and parks each call until its result arrives."""
        listed = mcp.types.ListToolsResult(
            tools=[
                mcp.types.Tool(
                    name=short_name(tool.name),
                    description=(tool.description or "")[:_DESCRIPTION_LIMIT],
                    input_schema=dict(tool.parameters),
                    # Claude Code runs calls that are not read-only one after another.
                    annotations=mcp.types.ToolAnnotations(read_only_hint=False),
                    _meta={"anthropic/alwaysLoad": True, "anthropic/maxResultSizeChars": 500_000},
                )
                for tool in tools
            ],
        )

        async def on_list_tools(_context: object, _params: object) -> mcp.types.ListToolsResult:
            return listed

        async def on_call_tool(_context: object, params: mcp.types.CallToolRequestParams) -> mcp.types.CallToolResult:
            tool_use_id = (params.meta or {}).get(_TOOL_USE_ID_META)
            if not isinstance(tool_use_id, str) or params.name not in self.names:
                # Never guess which call this is: the process is retired and the conversation rebuilt.
                reason = f"Claude Code called {params.name} without a usable call id"
                self.fail(reason)
                return mcp.types.CallToolResult(
                    content=[mcp.types.TextContent(type="text", text=reason)],
                    is_error=True,
                )
            self.entered[tool_use_id] = params.name
            return mcp.types.CallToolResult.model_validate(await self.result(tool_use_id))

        return Server(_SERVER_NAME, version=__version__, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


@dataclass(eq=False)
class LiveSession:
    """One Claude Code process and what it last answered with.

    ``called`` holds the tool of every call the model made in this process, by call id.
    """

    client: ClaudeSDKClient
    owner: asyncio.Task[None]
    close_requested: asyncio.Event
    bridge: ToolBridge
    directory: Path
    called: dict[str, str] = field(default_factory=dict)
    emitted: Emitted | None = None
    rested_at: float = 0.0

    def broken(self) -> str | None:
        """Return why the process cannot continue: its bridge failed, or a call reached it that the model never made."""
        if self.bridge.failure is not None:
            return self.bridge.failure
        strays = [call_id for call_id, name in self.bridge.entered.items() if self.called.get(call_id) != name]
        return f"Claude Code ran tool calls the model did not make: {', '.join(strays)}" if strays else None

    async def send(self, blocks: list[dict[str, Any]]) -> None:
        """Send one user message, which starts a turn."""

        async def messages() -> Any:  # noqa: ANN401 - the SDK takes an async iterable of message dicts
            yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}

        await self.client.query(messages())

    def resolve(self, results: dict[str, dict[str, Any]]) -> None:
        """Give each waiting tool call its result, which continues the turn."""
        for tool_use_id, result in results.items():
            self.bridge.result(tool_use_id).set_result(result)


def startup_error(info: dict[str, Any] | None, provider: str) -> AuthenticationFailed | None:
    """Return why the Claude Code that answered with ``info`` at start-up must not get a prompt, or None.

    Only a binary new enough to send prompts verbatim, signed in to a Claude subscription, passes: a credential
    the environment supplies would be billed or used instead of the user's subscription without anyone noticing.
    """
    info = info or {}
    version = str(info.get("claude_code_version") or "unknown")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    account = info.get("account") or {}
    key_source, token_source = account.get("apiKeySource"), account.get("tokenSource")
    minimum = ".".join(map(str, MIN_VERSION))
    if match is None or tuple(map(int, match.groups())) < MIN_VERSION:
        message = f"Claude Code {version} is too old; {minimum} or later is required. Run `claude update`."
    elif key_source:
        message = (
            f"Claude Code would use an API key ({key_source}) instead of your Claude subscription. Remove it from "
            "MindRoom's environment, or use `provider: anthropic` to pay with the key."
        )
    elif token_source == "none":  # noqa: S105 - Claude Code's word for no sign-in
        message = NOT_SIGNED_IN
    elif token_source not in (None, "claude.ai"):
        message = (
            f"Claude Code would use a token from {token_source} instead of your Claude sign-in. Remove it from "
            "MindRoom's environment and run `claude auth login`."
        )
    elif account.get("apiProvider") != "firstParty" or not account.get("subscriptionType"):
        message = f"Claude Code is not signed in to a Claude subscription. {NOT_SIGNED_IN.partition('. ')[2]}"
    else:
        return None
    return AuthenticationFailed(message, provider=provider, status_code=401)


async def start(
    *,
    model: str,
    settings: ClaudeCodeSettings,
    system: str,
    tools: Sequence[ToolSpec],
    storage_root: Path,
    provider: str,
) -> LiveSession:
    """Start a Claude Code process that has only ``tools``, and check that it is signed in to a subscription."""
    SESSIONS.make_room()
    root = storage_root / "claude_code"
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, dir=root))
    # The process runs in an empty directory of its own, so no project file or setting reaches it.
    cwd, system_file = directory / "cwd", directory / "system.txt"
    await asyncio.to_thread(cwd.mkdir)
    await asyncio.to_thread(system_file.write_text, system)
    bridge = ToolBridge(names=tool_names(tools))
    options = ClaudeAgentOptions(
        model=model,
        # The SDK otherwise prefers the copy it ships over the one the user signed in with and keeps updated.
        cli_path=settings.cli_path or shutil.which("claude"),
        effort=settings.effort,  # ty: ignore[invalid-argument-type]
        system_prompt={"type": "file", "path": str(system_file)} if system else "",
        cwd=cwd,
        tools=[],
        skills=[],
        setting_sources=[],
        strict_mcp_config=True,
        permission_mode="dontAsk",
        settings='{"disableAllHooks":true}',
        # Without it a user's ``@/path`` would make Claude Code read that file.
        verbatim_prompts=True,
        include_partial_messages=True,
        thinking={"type": "adaptive", "display": "summarized"},
        env=_ISOLATION_ENV,
        extra_args={"no-session-persistence": None},
        max_buffer_size=_MAX_MESSAGE_BYTES,
        client_app=f"mindroom/{__version__}",
        stderr=lambda line: logger.debug("claude_code_stderr", line=line),
    )
    if tools:
        options.mcp_servers = {_SERVER_NAME: {"type": "sdk", "name": _SERVER_NAME, "instance": bridge.server(tools)}}
        options.allowed_tools = [f"{TOOL_PREFIX}*"]
    client = ClaudeSDKClient(options=options)
    close_requested = asyncio.Event()
    try:
        owner = await open_client(client, close_requested, bridge.messages)
    except BaseException as error:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)
        if isinstance(error, CLINotFoundError):
            raise AuthenticationFailed(_NOT_FOUND, provider=provider) from error
        if isinstance(error, ClaudeSDKError | OSError):
            msg = f"Claude Code did not start: {error}"
            raise ProviderUnavailable(msg, provider=provider) from error
        raise
    session = LiveSession(
        client=client,
        owner=owner,
        close_requested=close_requested,
        bridge=bridge,
        directory=directory,
    )
    SESSIONS.live.add(session)
    if (error := startup_error(await client.get_server_info(), provider)) is not None:
        SESSIONS.retire(session)
        raise error
    return session


async def _close(session: LiveSession) -> None:
    """Stop one process and remove its directory; a tool call it still waits on is never answered."""
    session.close_requested.set()
    with suppress(Exception):
        await session.owner
    await asyncio.to_thread(shutil.rmtree, session.directory, ignore_errors=True)


class SessionRegistry:
    """Every live Claude Code process of this runtime, and the resting ones by the request each answered."""

    def __init__(self) -> None:
        self.live: set[LiveSession] = set()
        self.resting: dict[str, LiveSession] = {}
        self._sweep: asyncio.Task[None] | None = None

    def claim(self, key: str) -> LiveSession | None:
        """Take the process that rests under ``key``, so no other request continues it."""
        return self.resting.pop(key, None)

    def rest(self, key: str, session: LiveSession) -> None:
        """Keep ``session`` for the request that follows the answer it just gave."""
        if (replaced := self.resting.pop(key, None)) is not None:
            self.retire(replaced)
        session.rested_at = monotonic()
        self.resting[key] = session
        if self._sweep is None or self._sweep.done():
            self._sweep = create_background_task(self._sweep_expired(), name="claude_code_sweep", context=Context())

    def retire(self, session: LiveSession) -> None:
        """Close ``session`` on a task of its own, which a stopped request cannot cut short."""
        self.live.discard(session)
        create_background_task(_close(session), name="claude_code_close", owner=self, context=Context())

    def make_room(self) -> None:
        """Retire resting processes, idle ones first and the longest resting first, until a new one fits."""
        while len(self.live) >= MAX_PROCESSES and self.resting:
            key = min(self.resting, key=lambda key: (_parked(self.resting[key]), self.resting[key].rested_at))
            self.retire(self.resting.pop(key))

    async def close_expired(self) -> None:
        """Close each resting process past its time to live, and wait until they closed."""
        now = monotonic()
        for key, session in list(self.resting.items()):
            if now - session.rested_at > (PARKED_TTL_SECONDS if _parked(session) else IDLE_TTL_SECONDS):
                self.retire(self.resting.pop(key))
        await wait_for_background_tasks(owner=self)

    async def close_all(self) -> None:
        """Close every process, as orderly shutdown does."""
        self.resting.clear()
        for session in list(self.live):
            self.retire(session)
        if self._sweep is not None:
            self._sweep.cancel()
        await wait_for_background_tasks(owner=self)

    async def _sweep_expired(self) -> None:
        while self.resting:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            await self.close_expired()


def _parked(session: LiveSession) -> bool:
    return session.emitted is not None and bool(session.emitted.tool_call_ids)


SESSIONS = SessionRegistry()
close_at_process_shutdown(SESSIONS.close_all)
