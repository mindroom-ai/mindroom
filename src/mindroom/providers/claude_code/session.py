"""Live Claude Code processes of the ``claude_code`` route, and the MCP server that offers them MindRoom's tools.

A process is read by the one request that started or claimed it, or it rests in ``SESSIONS`` under the identity of
the request it answered: parked on tool calls whose results the next request brings, or idle after an answer. Its
tool calls reach an in-process MCP server whose handler only waits for the result MindRoom's own loop produces.
MindRoom's history stays the only record of a conversation: no process is resumed and none saves a transcript.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from contextvars import Context
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import mcp.types
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLINotFoundError,
    RateLimitEvent,
    SystemMessage,
)
from mcp.server import Server

from mindroom import __version__
from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.claude_sdk_process import open_client
from mindroom.llm.errors import AuthenticationFailed, ProviderError, ProviderUnavailable
from mindroom.logging_config import get_logger
from mindroom.providers.claude_code.prompt import TOOL_PREFIX, canonical_arguments, tool_names
from mindroom.runtime_shutdown import close_at_process_shutdown

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence
    from pathlib import Path

    from claude_agent_sdk import Message as ClaudeMessage

    from mindroom.llm.provider import ToolSpec
    from mindroom.providers.claude_code.prompt import Emitted
    from mindroom.providers.claude_code.settings import ClaudeCodeSettings

__all__ = ["ENVIRONMENT_OVERRIDES", "NOT_SIGNED_IN", "SERVER_NAME", "SESSIONS", "LiveSession", "start"]

logger = get_logger(__name__)

# Older binaries ignore verbatim prompts, so a user's ``@path`` or ``/command`` would be read as a Claude Code command.
_MIN_VERSION = (2, 1, 248)
# The request metadata Claude Code sends with an MCP tool call: the id of the ``tool_use`` block that made the call.
_TOOL_USE_ID_META = "claudecode/toolUseId"
SERVER_NAME = "mindroom"
_CWD, _SYSTEM_FILE = "cwd", "system.txt"
_PARKED_TTL_SECONDS = 30 * 60.0
_IDLE_TTL_SECONDS = 5 * 60.0
# A process takes a few hundred megabytes. One that answers a request is never closed to make room for another.
_MAX_PROCESSES = 8
_SWEEP_INTERVAL_SECONDS = 60.0
# How long a tool call waits for the model message that made it, which Claude Code sends before the call.
_UNMADE_CALL_SECONDS = 30.0
# How long Claude Code may take to connect MindRoom's tool server before it gets its first request.
_TOOLS_CONNECT_SECONDS = 30.0
# A tool result that holds images comes back from Claude Code as one message.
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Claude Code cuts a longer MCP tool description itself.
_DESCRIPTION_LIMIT = 2048
_CUT_DESCRIPTIONS: set[str] = set()
# The directories of this runtime's processes start with this, so the ones an earlier runtime left can be told apart.
_RUNTIME = uuid4().hex[:8]
# Claude Code features that would duplicate MindRoom's or hide its tools: memory files, compaction, and tool search.
_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # MindRoom already bounds its tool results; Claude Code would replace a longer one with a file it cannot read.
    "MAX_MCP_OUTPUT_TOKENS": "200000",
    # A parked call waits for MindRoom's loop, such as for an approval: Claude Code must outwait the parked process.
    "MCP_TOOL_TIMEOUT": str(int((_PARKED_TTL_SECONDS + 10 * _SWEEP_INTERVAL_SECONDS) * 1000)),
}
# What Claude Code reads from its environment in place of the Claude sign-in, or to send requests somewhere else.
ENVIRONMENT_OVERRIDES = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_UNIX_SOCKET",
    "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
]
_ANTHROPIC_URL = "https://api.anthropic.com"
# How Claude Code names the plans of a Claude subscription; an account without one is "Claude API".
_PLANS = ("Claude Pro", "Claude Max", "Claude Team", "Claude Enterprise")
NOT_SIGNED_IN = "Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom."
_NOT_FOUND = (
    "Claude Code was not found. Install it (https://claude.com/claude-code), run `claude auth login` as the user "
    "that runs MindRoom, and set `extra_kwargs.cli_path` if `claude` is not on MindRoom's PATH."
)
_OTHER_TOOLS = "Claude Code did not connect exactly MindRoom's tools, so it got no request."


@dataclass(eq=False)
class _Call:
    """One tool call a process waits on: the tool and arguments the model wrote, and the result MindRoom gives."""

    result: asyncio.Future[dict[str, Any]]
    made: tuple[str, str] | None = None


@dataclass(eq=False)
class _ToolBridge:
    """The tool calls of one Claude Code process: each waits here for the result MindRoom's loop produces.

    ``calls`` holds the calls that wait, by Claude Code's call id, and ``entered`` the id of every call that reached
    the tool server. ``failure`` says why the process can no longer be trusted, such as a call the model did not write.
    """

    names: dict[str, str]
    messages: asyncio.Queue[ClaudeMessage | Exception] = field(default_factory=asyncio.Queue)
    calls: dict[str, _Call] = field(default_factory=dict)
    entered: set[str] = field(default_factory=set)
    failure: str | None = None

    def _call(self, tool_use_id: str) -> _Call:
        if tool_use_id not in self.calls:
            self.calls[tool_use_id] = _Call(asyncio.get_running_loop().create_future())
        return self.calls[tool_use_id]

    def made(self, tool_use_id: str, name: str, arguments: object) -> None:
        """Keep a tool call the model wrote, which Claude Code then brings to the tool server."""
        self._call(tool_use_id).made = (name, canonical_arguments(arguments))

    def resolve(self, results: dict[str, dict[str, Any]]) -> None:
        """Give each waiting tool call its result, which continues the turn."""
        for tool_use_id, result in results.items():
            call = self.calls.get(tool_use_id)
            # A call Claude Code gave up meanwhile has failed the process, which the request that reads it learns.
            if call is not None and not call.result.done():
                call.result.set_result(result)

    def fail(self, reason: str) -> None:
        """Mark the process as broken and wake the request that reads it."""
        if self.failure is None:
            self.failure = reason
            self.messages.put_nowait(ClaudeSDKError(reason))

    def _refuse(self, reason: str) -> mcp.types.CallToolResult:
        """Fail the process and answer a call with ``reason`` instead of a result: no call is ever guessed."""
        self.fail(reason)
        return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=reason)], is_error=True)

    async def _answer(self, params: mcp.types.CallToolRequestParams) -> mcp.types.CallToolResult:
        """Wait for the result of the call the model wrote under this call's id, and for no other call."""
        tool_use_id = (params.meta or {}).get(_TOOL_USE_ID_META)
        if not isinstance(tool_use_id, str) or params.name not in self.names:
            return self._refuse(f"Claude Code called {params.name} without a usable call id")
        if tool_use_id in self.entered:
            return self._refuse(f"Claude Code made the tool call {tool_use_id} twice")
        self.entered.add(tool_use_id)
        call = self._call(tool_use_id)
        try:
            try:
                async with asyncio.timeout(_UNMADE_CALL_SECONDS):
                    while call.made is None:  # noqa: ASYNC110 - only a call that outran its model message waits
                        await asyncio.sleep(0.02)
            except TimeoutError:
                return self._refuse(f"Claude Code made a tool call the model did not write: {tool_use_id}")
            if call.made != (params.name, canonical_arguments(params.arguments or {})):
                return self._refuse(f"Claude Code made the tool call {tool_use_id} unlike the model wrote it")
            return mcp.types.CallToolResult.model_validate(await call.result)
        except asyncio.CancelledError:
            self.fail(f"Claude Code gave up the tool call {tool_use_id}")
            raise
        finally:
            # A result is given once: what a later call under this id presents no longer finds it.
            del self.calls[tool_use_id]

    def server(self, tools: Sequence[ToolSpec]) -> Server:
        """Return the MCP server that offers ``tools`` and parks each call until its result arrives."""
        listed = mcp.types.ListToolsResult(
            tools=[
                mcp.types.Tool(
                    name=short,
                    description=_description(tool),
                    input_schema=dict(tool.parameters),
                    # Claude Code runs calls that are not read-only one after another.
                    annotations=mcp.types.ToolAnnotations(read_only_hint=False),
                    _meta={"anthropic/alwaysLoad": True, "anthropic/maxResultSizeChars": 500_000},
                )
                for short, tool in zip(self.names, tools, strict=True)
            ],
        )

        async def on_list_tools(_context: object, _params: object) -> mcp.types.ListToolsResult:
            return listed

        async def on_call_tool(_context: object, params: mcp.types.CallToolRequestParams) -> mcp.types.CallToolResult:
            return await self._answer(params)

        return Server(SERVER_NAME, version=__version__, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _description(tool: ToolSpec) -> str:
    """Return the description Claude Code takes for ``tool``, saying once when it loses its end."""
    description = tool.description or ""
    if len(description) > _DESCRIPTION_LIMIT and tool.name not in _CUT_DESCRIPTIONS:
        _CUT_DESCRIPTIONS.add(tool.name)
        logger.warning("claude_code_tool_description_cut", tool=tool.name, length=len(description))
    return description[:_DESCRIPTION_LIMIT]


@dataclass(eq=False)
class LiveSession:
    """One Claude Code process and what it last answered with.

    ``inventoried`` says the process started a turn with exactly MindRoom's tools, before which none of its output
    counts.
    """

    client: ClaudeSDKClient
    owner: asyncio.Task[None]
    close_requested: asyncio.Event
    bridge: _ToolBridge
    directory: Path
    provider: str
    inventoried: bool = False
    emitted: Emitted | None = None
    rested_at: float = 0.0

    def broken(self) -> str | None:
        """Return why the resting process cannot be continued, or None.

        It cannot once its tool server failed, and once it sent anything but notices while it rested: it exited, or
        it answered a tool call itself and went on without MindRoom's result.
        """
        bridge = self.bridge
        while bridge.failure is None and not bridge.messages.empty():
            if not isinstance(message := bridge.messages.get_nowait(), SystemMessage | RateLimitEvent):
                bridge.failure = f"Claude Code did not wait for MindRoom ({type(message).__name__})"
        if bridge.failure is None and any(call.made is None for call in bridge.calls.values()):
            bridge.failure = "Claude Code made a tool call the model did not write"
        return bridge.failure

    async def send(self, blocks: list[dict[str, Any]]) -> None:
        """Send one user message, which starts a turn."""

        async def messages() -> Any:  # noqa: ANN401 - the SDK takes an async iterable of message dicts
            yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}

        try:
            await self.client.query(messages())
        except ClaudeSDKError as error:
            msg = f"Claude Code stopped: {error}"
            raise ProviderUnavailable(msg, provider=self.provider) from error


def _environment_error(provider: str) -> AuthenticationFailed | None:
    """Return why Claude Code must not start in MindRoom's environment, naming its variables and never their values."""
    found = [
        name
        for name in ENVIRONMENT_OVERRIDES
        if (value := os.environ.get(name)) and (name, value.rstrip("/")) != ("ANTHROPIC_BASE_URL", _ANTHROPIC_URL)
    ]
    if not found:
        return None
    message = (
        f"MindRoom's environment sets {', '.join(found)}, which Claude Code would use in place of your Claude "
        "sign-in or to send it somewhere else. Remove it from the environment MindRoom starts in (the `.env` beside "
        "`config.yaml` is fine), or use `provider: anthropic`."
    )
    return AuthenticationFailed(message, provider=provider, status_code=401)


def _startup_error(info: dict[str, Any] | None, provider: str) -> AuthenticationFailed | None:
    """Return why the Claude Code that answered with ``info`` at start-up must not get a prompt, or None.

    Only a binary new enough to send prompts verbatim, signed in to a plan of a Claude subscription, passes: a
    credential the environment supplies would be billed or used instead of the subscription without anyone noticing.
    """
    info = info or {}
    version = str(info.get("claude_code_version") or "unknown")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    account = info.get("account") or {}
    key_source, token_source = account.get("apiKeySource"), account.get("tokenSource")
    minimum = ".".join(map(str, _MIN_VERSION))
    if match is None or tuple(map(int, match.groups())) < _MIN_VERSION:
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
    elif account.get("apiProvider") != "firstParty" or not str(account.get("subscriptionType")).startswith(_PLANS):
        message = f"Claude Code is not signed in to a Claude subscription. {NOT_SIGNED_IN.partition('. ')[2]}"
    else:
        return None
    return AuthenticationFailed(message, provider=provider, status_code=401)


async def _refuse_unless_ready(
    client: ClaudeSDKClient,
    offered: list[str],
    cli_path: str | None,
    provider: str,
) -> None:
    """Raise unless Claude Code may get a prompt: signed in to a subscription, with exactly the ``offered`` tools.

    Its tool server connects a moment after it starts, so that is awaited; no other server may be connected.
    """
    info = await client.get_server_info()
    logger.debug("claude_code_started", cli=cli_path or "bundled", version=(info or {}).get("claude_code_version"))
    if (refused := _startup_error(info, provider)) is not None:
        raise refused
    wanted = {SERVER_NAME: ("connected", sorted(offered))} if offered else {}
    deadline = monotonic() + _TOOLS_CONNECT_SECONDS
    while True:
        servers = (await client.get_mcp_status()).get("mcpServers") or ()
        found = {
            server.get("name"): (server.get("status"), sorted(tool.get("name", "") for tool in server.get("tools", ())))
            for server in servers
        }
        if found == wanted:
            return
        connecting = set(found) <= set(wanted) and all(status == "pending" for status, _tools in found.values())
        if not connecting or monotonic() > deadline:
            raise ProviderError(_OTHER_TOOLS, provider=provider)
        await asyncio.sleep(0.05)


def _make_directory(directory: Path, system: str, *, first: bool) -> None:
    """Make the directory of one process: an empty working directory, and its system prompt beside it.

    The process runs where no project file or setting reaches it, and the prompt goes in a file because a long one
    does not fit a command line. The ``first`` process of a runtime removes what earlier runtimes left behind, such
    as the prompts of processes that were killed.
    """
    directory.parent.mkdir(parents=True, exist_ok=True)
    for left in directory.parent.iterdir() if first else ():
        if not left.name.startswith(_RUNTIME):
            shutil.rmtree(left, ignore_errors=True)
    (directory / _CWD).mkdir(parents=True)
    (directory / _SYSTEM_FILE).write_text(system, encoding="utf-8")


async def _remove(directory: Path, made: asyncio.Future[None]) -> None:
    """Remove the directory of a process that did not start, once the thread that makes it is done."""
    await asyncio.wait({made})
    await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


async def start(
    *,
    model: str,
    cli_path: str | None,
    settings: ClaudeCodeSettings,
    storage_root: Path,
    provider: str,
    thinking: bool = True,
    system: str = "",
    tools: Sequence[ToolSpec] = (),
) -> LiveSession:
    """Start a Claude Code process that has only ``tools``, and check that it is signed in to a subscription.

    ``cli_path`` is the executable to run, or None for the copy the SDK ships. From the slot it takes on, one
    background task ends what a failed or cancelled start leaves: the process, its directory, and the slot.
    """
    if (refused := _environment_error(provider)) is not None:
        raise refused
    evicted = SESSIONS.reserve(provider)
    root = storage_root / "claude_code"
    directory = root / f"{_RUNTIME}-{uuid4().hex}"
    made = asyncio.ensure_future(
        asyncio.to_thread(_make_directory, directory, system, first=root not in SESSIONS.roots),
    )
    SESSIONS.roots.add(root)
    session: LiveSession | None = None
    try:
        if evicted is not None:
            await asyncio.shield(evicted)
        await asyncio.shield(made)
        bridge = _ToolBridge(names=tool_names(tools, provider))
        options = ClaudeAgentOptions(
            model=model,
            cli_path=cli_path,
            effort=settings.effort,
            system_prompt={"type": "file", "path": str(directory / _SYSTEM_FILE)} if system else "",
            cwd=directory / _CWD,
            tools=[],
            skills=[],
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            settings='{"disableAllHooks":true}',
            # Without it a user's ``@/path`` would make Claude Code read that file.
            verbatim_prompts=True,
            include_partial_messages=True,
            thinking={"type": "adaptive", "display": "summarized"} if thinking else None,
            env=_ISOLATION_ENV,
            extra_args={"no-session-persistence": None},
            max_buffer_size=_MAX_MESSAGE_BYTES,
            client_app=f"mindroom/{__version__}",
            stderr=lambda line: logger.debug("claude_code_stderr", line=line),
        )
        if tools:
            options.mcp_servers = {SERVER_NAME: {"type": "sdk", "name": SERVER_NAME, "instance": bridge.server(tools)}}
            options.allowed_tools = [f"{TOOL_PREFIX}*"]
        client = ClaudeSDKClient(options=options)
        close_requested = asyncio.Event()
        owner = await open_client(client, close_requested, bridge.messages)
        session = LiveSession(client, owner, close_requested, bridge, directory, provider)
        SESSIONS.live.add(session)
        await _refuse_unless_ready(client, list(bridge.names), cli_path, provider)
    except BaseException as error:
        if session is None:
            SESSIONS.release_after(_remove(directory, made))
        else:
            SESSIONS.retire(session)
        if isinstance(error, CLINotFoundError):
            raise AuthenticationFailed(_NOT_FOUND, provider=provider) from error
        if isinstance(error, ClaudeSDKError | OSError):
            msg = f"Claude Code did not start: {error}"
            raise ProviderUnavailable(msg, provider=provider) from error
        raise
    return session


async def _close(session: LiveSession) -> None:
    """Stop one process and remove its directory; a tool call it still waits on is never answered."""
    session.close_requested.set()
    await asyncio.wait({session.owner})
    await asyncio.to_thread(shutil.rmtree, session.directory, ignore_errors=True)


class _SessionRegistry:
    """Every Claude Code process of this runtime, and the resting ones by the request each answered.

    ``slots`` counts the processes that exist, from before one starts until it is gone, and never passes the limit.
    """

    def __init__(self) -> None:
        self.live: set[LiveSession] = set()
        self.resting: dict[str, LiveSession] = {}
        self.slots = 0
        self.roots: set[Path] = set()
        self._sweep: asyncio.Task[None] | None = None

    def reserve(self, provider: str) -> asyncio.Task[None] | None:
        """Take the slot of a new process, before its start first waits for anything.

        At the limit, the slot is that of a resting process this closes, an idle one before a parked one and the
        longest resting first: the start awaits the returned close. With every process answering a request, the
        new one is refused.
        """
        if self.slots < _MAX_PROCESSES:
            self.slots += 1
            return None
        if not self.resting:
            msg = f"MindRoom already runs its limit of {self.slots} Claude Code processes. Try again shortly."
            raise ProviderUnavailable(msg, provider=provider)
        key = min(self.resting, key=lambda key: (_parked(self.resting[key]), self.resting[key].rested_at))
        return self.retire(self.resting.pop(key), release=False)

    def release_after(self, teardown: Coroutine[Any, Any, None], *, release: bool = True) -> asyncio.Task[None]:
        """Run ``teardown`` on a task of its own, which a stopped request cannot cut short, then free its slot."""

        async def run() -> None:
            try:
                await teardown
            finally:
                if release:
                    self.slots -= 1

        return create_background_task(run(), name="claude_code_close", owner=self, context=Context())

    def retire(self, session: LiveSession, *, release: bool = True) -> asyncio.Task[None]:
        """Close ``session``; its slot goes to the process that evicted it unless ``release``."""
        self.live.discard(session)
        return self.release_after(_close(session), release=release)

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

    def retire_waiting_on(self, call_ids: set[str]) -> None:
        """Close each resting process that waits on one of ``call_ids``, which a request holds as history."""
        for key, session in list(self.resting.items()):
            if session.emitted is not None and call_ids.intersection(session.emitted.tool_call_ids):
                self.retire(self.resting.pop(key))

    async def close_expired(self) -> None:
        """Close each resting process past its time to live, and wait until they closed."""
        now = monotonic()
        for key, session in list(self.resting.items()):
            if now - session.rested_at > (_PARKED_TTL_SECONDS if _parked(session) else _IDLE_TTL_SECONDS):
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


SESSIONS = _SessionRegistry()
close_at_process_shutdown(SESSIONS.close_all)
