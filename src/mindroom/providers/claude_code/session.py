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
import tempfile
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Sequence

    from claude_agent_sdk import Message as ClaudeMessage
    from claude_agent_sdk import RateLimitInfo

    from mindroom.llm.provider import ToolSpec
    from mindroom.providers.claude_code.prompt import Emitted

__all__ = ["NOT_SIGNED_IN", "SERVER_NAME", "SESSIONS", "LiveSession", "RestingKey", "start"]

logger = get_logger(__name__)

# Older binaries ignore verbatim prompts, so a user's ``@path`` or ``/command`` would be read as a Claude Code command.
_MIN_VERSION = (2, 1, 248)
# The request metadata Claude Code sends with an MCP tool call: the id of the ``tool_use`` block that made the call.
_TOOL_USE_ID_META = "claudecode/toolUseId"
SERVER_NAME = "mindroom"
# Only this user reads this runtime's directory, which goes when MindRoom exits. Every process runs in its empty
# ``cwd``, where no project file or setting reaches it, and its system prompt lies beside that while it starts.
_HOME = tempfile.TemporaryDirectory(prefix="mindroom-claude-code-")
_CWD = Path(_HOME.name, "cwd")
_CWD.mkdir()
_PARKED_TTL_SECONDS = 30 * 60.0
_IDLE_TTL_SECONDS = 5 * 60.0
# A process takes a few hundred megabytes. One that answers a request is never closed to make room for another.
_MAX_PROCESSES = 8
_SWEEP_INTERVAL_SECONDS = 60.0
# How long a tool call waits for the model message that made it, which Claude Code sends before the call.
_UNMADE_CALL_SECONDS = 30.0
# How long Claude Code may take to say that it is ready, with MindRoom's tool server connected.
_READY_SECONDS = 30.0
# A tool result that holds images comes back from Claude Code as one message.
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Claude Code cuts a longer MCP tool description itself.
_DESCRIPTION_LIMIT = 2048
_CUT_DESCRIPTIONS: set[str] = set()
# Claude Code features that would duplicate MindRoom's or hide its tools: memory files, compaction, and tool search.
_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # A call that waits long is not moved to the background, where the model would go on without its result.
    "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND": "1",
    "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
    # MindRoom already bounds its tool results; Claude Code would replace a longer one with a file it cannot read.
    "MAX_MCP_OUTPUT_TOKENS": "200000",
    # A parked call waits for MindRoom's loop, such as for an approval: Claude Code must outwait the parked process.
    "MCP_TOOL_TIMEOUT": str(int((_PARKED_TTL_SECONDS + 10 * _SWEEP_INTERVAL_SECONDS) * 1000)),
}
# Claude Code reads its sign-in, where it sends requests, and what it adds to them from variables named like these.
_REFUSED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_REFUSED_NAMES = {"CLAUDE_BG_AUTH_SNAPSHOT_PATH", "CLAUDE_AUTO_BACKGROUND_TASKS"}
# The Claude Agent SDK and MindRoom set these for the process themselves, whatever MindRoom inherited.
_SET_FOR_THE_PROCESS = {"CLAUDE_CODE_ENTRYPOINT", *_ISOLATION_ENV}
# How Claude Code names the plans of a Claude subscription; an account without one is "Claude API".
_PLAN = re.compile(r"Claude (Pro|Max|Team|Enterprise)\b")
NOT_SIGNED_IN = "Claude Code is not signed in. Run `claude auth login` as the user that runs MindRoom."
_NOT_FOUND = (
    "Claude Code was not found. Install it (https://claude.com/claude-code), run `claude auth login` as the user "
    "that runs MindRoom, and set `extra_kwargs.cli_path` if `claude` is not on MindRoom's PATH."
)
_OTHER_TOOLS = "Claude Code did not connect exactly MindRoom's tools, so it got no request."


@dataclass(eq=False)
class _Call:
    """One tool call a process waits on: what the model wrote, the result MindRoom gives, and whether it was made."""

    made: asyncio.Future[tuple[str, str]]
    result: asyncio.Future[dict[str, Any]]
    entered: bool = False


@dataclass(eq=False)
class _ToolBridge:
    """The tool calls of one Claude Code process, by call id: each waits for the result MindRoom's loop produces.

    ``calls`` holds a call until its result was handed to Claude Code, ``unconfirmed`` its id until Claude Code said
    that it took the result, and ``spent`` its id for good. ``failure`` says why the process can no longer be
    trusted, such as a call the model did not write.
    """

    names: dict[str, str]
    messages: asyncio.Queue[ClaudeMessage | Exception] = field(default_factory=asyncio.Queue)
    calls: dict[str, _Call] = field(default_factory=dict)
    unconfirmed: set[str] = field(default_factory=set)
    spent: set[str] = field(default_factory=set)
    failure: str | None = None

    def _call(self, tool_use_id: str) -> _Call:
        if tool_use_id not in self.calls:
            loop = asyncio.get_running_loop()
            self.calls[tool_use_id] = _Call(loop.create_future(), loop.create_future())
        return self.calls[tool_use_id]

    def made(self, tool_use_id: str, name: str, arguments: object) -> bool:
        """Keep a tool call the model wrote, which Claude Code then brings to the tool server; False for a used id."""
        if tool_use_id in self.spent or (call := self._call(tool_use_id)).made.done():
            return False
        call.made.set_result((name, canonical_arguments(arguments)))
        return True

    def resolve(self, results: dict[str, dict[str, Any]]) -> None:
        """Give each waiting tool call its result, which continues the turn."""
        for tool_use_id, result in results.items():
            # Claude Code can give a call up at any moment, which the request that reads the process then learns.
            if not (call := self.calls[tool_use_id]).result.done():
                call.result.set_result(result)

    @property
    def undelivered(self) -> bool:
        """Whether Claude Code has not taken a result MindRoom gave, without which its model must not go on."""
        return bool(self.unconfirmed) or any(call.result.done() for call in self.calls.values())

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
        if tool_use_id in self.spent or (call := self._call(tool_use_id)).entered:
            return self._refuse(f"Claude Code made the tool call {tool_use_id} twice")
        call.entered = True
        try:
            try:
                # Only a call that outran its model message waits here.
                made = await asyncio.wait_for(asyncio.shield(call.made), _UNMADE_CALL_SECONDS)
            except TimeoutError:
                return self._refuse(f"Claude Code made a tool call the model did not write: {tool_use_id}")
            if made != (params.name, canonical_arguments(params.arguments or {})):
                return self._refuse(f"Claude Code made the tool call {tool_use_id} unlike the model wrote it")
            result = mcp.types.CallToolResult.model_validate(await call.result)
        except asyncio.CancelledError:
            self.fail(f"Claude Code gave up the tool call {tool_use_id}")
            raise
        # Nothing of a call that is done is kept but its id, which no later call may use.
        del self.calls[tool_use_id]
        self.spent.add(tool_use_id)
        self.unconfirmed.add(tool_use_id)
        return result

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

    ``inventoried`` says the turn it runs started with exactly MindRoom's tools, before which none of its output
    counts. ``counted`` sums the usage its model messages reported in that turn, and ``rejected`` is the plan limit
    it last reported.
    """

    client: ClaudeSDKClient
    owner: asyncio.Task[None]
    close_requested: asyncio.Event
    bridge: _ToolBridge
    provider: str
    inventoried: bool = False
    counted: dict[str, int] = field(default_factory=dict)
    rejected: RateLimitInfo | None = None
    emitted: Emitted | None = None
    rested_at: float = 0.0

    def limited(self, event: RateLimitEvent) -> None:
        """Keep the plan limit Claude Code reports, which the result of a refused request does not repeat."""
        self.rejected = event.rate_limit_info if event.rate_limit_info.status == "rejected" else None

    def broken(self) -> str | None:
        """Return why the resting process cannot be continued, or None.

        It cannot once its tool server failed, and once it sent anything but notices while it rested: it exited, or
        it answered a tool call itself and went on without MindRoom's result.
        """
        bridge = self.bridge
        while bridge.failure is None and not bridge.messages.empty():
            message = bridge.messages.get_nowait()
            if isinstance(message, RateLimitEvent):
                self.limited(message)
            elif not isinstance(message, SystemMessage):
                bridge.failure = f"Claude Code did not wait for MindRoom ({type(message).__name__})"
        if bridge.failure is None and not all(call.made.done() for call in bridge.calls.values()):
            bridge.failure = "Claude Code made a tool call the model did not write"
        return bridge.failure

    async def send(self, blocks: list[dict[str, Any]]) -> None:
        """Send one user message, which starts a turn."""

        async def messages() -> Any:  # noqa: ANN401 - the SDK takes an async iterable of message dicts
            yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}

        self.inventoried, self.counted = False, {}
        try:
            await self.client.query(messages())
        except ClaudeSDKError as error:
            msg = f"Claude Code stopped: {error}"
            raise ProviderUnavailable(msg, provider=self.provider) from error


def _startup_error(info: dict[str, Any] | None, provider: str) -> AuthenticationFailed | None:
    """Return why the Claude Code that answered with ``info`` at start-up must not get a prompt, or None.

    Only a binary that sends prompts verbatim, signed in to a plan of a Claude subscription and nothing else, passes.
    """
    info = info or {}
    version = str(info.get("claude_code_version") or "unknown")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    account = info.get("account") or {}
    other = account.get("apiKeySource") or account.get("tokenSource")
    if match is None or tuple(map(int, match.groups())) < _MIN_VERSION:
        minimum = ".".join(map(str, _MIN_VERSION))
        message = f"Claude Code {version} is too old; {minimum} or later is required. Run `claude update`."
    elif other == "none":
        message = NOT_SIGNED_IN
    elif other not in (None, "claude.ai"):
        message = (
            f"Claude Code would use a credential from {other} instead of your Claude sign-in. Remove it from "
            "MindRoom's environment and run `claude auth login`, or use `provider: anthropic` to pay with a key."
        )
    elif account.get("apiProvider") != "firstParty" or not _PLAN.match(str(account.get("subscriptionType"))):
        message = f"Claude Code is not signed in to a Claude subscription. {NOT_SIGNED_IN.partition('. ')[2]}"
    else:
        return None
    return AuthenticationFailed(message, provider=provider, status_code=401)


async def _refuse_unless_ready(client: ClaudeSDKClient, offered: list[str], provider: str) -> None:
    """Raise unless Claude Code may get a prompt: signed in to a subscription, with exactly the ``offered`` tools.

    Its tool server connects a moment after it starts, so that is awaited.
    """
    info = await client.get_server_info()
    # The rest of what Claude Code says at start-up names the account, which is never logged.
    version = (info or {}).get("claude_code_version")
    logger.debug("claude_code_started", cli=client.options.cli_path or "bundled", version=version)
    if (refused := _startup_error(info, provider)) is not None:
        raise refused
    wanted = {SERVER_NAME: ("connected", sorted(offered))} if offered else {}
    while True:
        servers = (await client.get_mcp_status()).get("mcpServers")
        if not isinstance(servers, list):
            raise ProviderError(_OTHER_TOOLS, provider=provider)
        found = {
            server.get("name"): (server.get("status"), sorted(tool.get("name", "") for tool in server.get("tools", ())))
            for server in servers
        }
        if found == wanted:
            return
        if not set(found) <= set(wanted) or any(status != "pending" for status, _tools in found.values()):
            raise ProviderError(_OTHER_TOOLS, provider=provider)
        await asyncio.sleep(0.05)


def _write_prompt(system: str) -> Path:
    """Write a system prompt, too long for a command line, to a file only this user can read."""
    descriptor, path = tempfile.mkstemp(dir=_HOME.name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(system)
    return Path(path)


async def start(
    *,
    provider: str,
    system: str = "",
    tools: Sequence[ToolSpec] = (),
    replaces: LiveSession | None = None,
    **options: Any,  # noqa: ANN401 - fields of ``ClaudeAgentOptions``
) -> LiveSession:
    """Start a Claude Code process that has only ``tools``, and check that it is signed in to a subscription.

    The process takes the slot of ``replaces``, which this closes, or one of its own. Its start runs on a task no
    stopped request cuts short, since only the end of a start says whether a process holds the slot.
    """
    # MindRoom's environment is judged by the names of its variables alone, never by their values.
    refused = sorted(
        name
        for name in os.environ
        if (name.startswith(_REFUSED_PREFIXES) or name in _REFUSED_NAMES) and name not in _SET_FOR_THE_PROCESS
    )
    if refused:
        msg = (
            f"MindRoom's environment sets {', '.join(refused)}, which Claude Code could use in place of your Claude "
            "sign-in or to send it somewhere else. Remove it from the environment MindRoom starts in (the `.env` "
            "beside `config.yaml` is fine), or use `provider: anthropic`."
        )
        raise AuthenticationFailed(msg, provider=provider, status_code=401)
    freed = SESSIONS.reserve(provider, replaces)
    starting = create_background_task(
        _launch(freed, provider, system, tools, options),
        name="claude_code_start",
        owner=SESSIONS,
        context=Context(),
        # The request that waits for the start raises its failure.
        log_exceptions=False,
    )
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError:
        # Nothing waits for the process any more, so it closes once its start ended.
        starting.add_done_callback(lambda done: done.cancelled() or done.exception() or SESSIONS.retire(done.result()))
        raise


async def _launch(
    freed: asyncio.Task[None] | None,
    provider: str,
    system: str,
    tools: Sequence[ToolSpec],
    options: dict[str, Any],
) -> LiveSession:
    """Start one process in the slot ``start`` took, once ``freed`` ended, and give the slot up if that fails."""
    session = prompt = None
    try:
        if freed is not None:
            await freed
        bridge = _ToolBridge(names=tool_names(tools, provider))
        if system:
            prompt = await asyncio.to_thread(_write_prompt, system)
        sdk_options = ClaudeAgentOptions(
            **options,
            system_prompt={"type": "file", "path": str(prompt)} if prompt else "",
            cwd=_CWD,
            tools=[],
            skills=[],
            setting_sources=[],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            settings='{"disableAllHooks":true}',
            # Without it a user's ``@/path`` would make Claude Code read that file.
            verbatim_prompts=True,
            include_partial_messages=True,
            env=_ISOLATION_ENV,
            extra_args={"no-session-persistence": None},
            max_buffer_size=_MAX_MESSAGE_BYTES,
            client_app=f"mindroom/{__version__}",
            stderr=lambda line: logger.debug("claude_code_stderr", line=line),
        )
        if tools:
            server = bridge.server(tools)
            sdk_options.mcp_servers = {SERVER_NAME: {"type": "sdk", "name": SERVER_NAME, "instance": server}}
            sdk_options.allowed_tools = [f"{TOOL_PREFIX}*"]
        client = ClaudeSDKClient(options=sdk_options)
        close_requested = asyncio.Event()
        owner = await open_client(client, close_requested, bridge.messages)
        # From here the process holds the slot its start took.
        session = LiveSession(client, owner, close_requested, bridge, provider)
        SESSIONS.live.add(session)
        SESSIONS.starting -= 1
        async with asyncio.timeout(_READY_SECONDS):
            await _refuse_unless_ready(client, list(bridge.names), provider)
    except BaseException as error:
        if session is None:
            SESSIONS.starting -= 1
        else:
            await SESSIONS.retire(session)
        if (failure := _start_failure(error, provider)) is not None:
            raise failure from error
        raise
    finally:
        # Claude Code read the prompt when it started.
        if prompt is not None:
            await asyncio.to_thread(prompt.unlink, missing_ok=True)
    return session


def _start_failure(error: BaseException, provider: str) -> ProviderError | None:
    """Return the error of a start that ``error`` ended, or None for one that is raised as it is."""
    if isinstance(error, CLINotFoundError):
        return AuthenticationFailed(_NOT_FOUND, provider=provider)
    # The SDK raises a plain exception for a request about its state that Claude Code rejects.
    if isinstance(error, Exception) and not isinstance(error, ProviderError):
        return ProviderUnavailable(f"Claude Code did not start: {error or type(error).__name__}", provider=provider)
    return None


async def _close(session: LiveSession) -> None:
    """Stop one process; a tool call it still waits on is never answered."""
    session.close_requested.set()
    with suppress(Exception):
        await session.owner


# What a resting process is kept under: its conversation, the request it answered, and the calls it waits on.
type RestingKey = tuple[str, str, tuple[str, ...]]


class _SessionRegistry:
    """Every Claude Code process of this runtime, and the resting ones by the request each answered.

    A process holds a slot until it is gone: ``starting`` counts the starts without one yet, ``live`` holds the rest.
    """

    def __init__(self) -> None:
        self.live: set[LiveSession] = set()
        self.resting: dict[RestingKey, LiveSession] = {}
        self.starting = 0
        self._sweep: asyncio.Task[None] | None = None

    def reserve(self, provider: str, replaced: LiveSession | None) -> asyncio.Task[None] | None:
        """Take the slot of a new process, and return the end of the process whose slot it is, which the start awaits.

        That is ``replaced``, or at the limit a resting one this closes, an idle one before a parked one and the
        longest resting first. With every process answering a request, the new one is refused.
        """
        if replaced is None and len(self.live) + self.starting >= _MAX_PROCESSES:
            if not self.resting:
                msg = f"MindRoom already runs its limit of {_MAX_PROCESSES} Claude Code processes. Try again shortly."
                raise ProviderUnavailable(msg, provider=provider)
            key = min(self.resting, key=lambda key: (bool(key[2]), self.resting[key].rested_at))
            replaced = self.resting.pop(key)
        self.starting += 1
        return None if replaced is None else self.retire(replaced)

    def retire(self, session: LiveSession) -> asyncio.Task[None]:
        """Close ``session`` on a task no stopped request cuts short; it holds its slot until it is gone."""

        async def run() -> None:
            try:
                await _close(session)
            finally:
                self.live.discard(session)

        return create_background_task(run(), name="claude_code_close", owner=self, context=Context())

    def rest(self, key: RestingKey, session: LiveSession) -> None:
        """Keep ``session`` for the request that follows the answer it just gave."""
        if (replaced := self.resting.pop(key, None)) is not None:
            self.retire(replaced)
        session.rested_at = monotonic()
        self.resting[key] = session
        if self._sweep is None or self._sweep.done():
            self._sweep = create_background_task(self._sweep_expired(), name="claude_code_sweep", context=Context())

    def retire_superseded(self, conversation: str, call_ids: set[str]) -> None:
        """Close what a request that starts over supersedes: an idle process, or one that waits on ``call_ids``."""
        for key in list(self.resting):
            if key[0] == conversation and (not key[2] or call_ids.intersection(key[2])):
                self.retire(self.resting.pop(key))

    async def close_expired(self) -> None:
        """Close each resting process past its time to live, and wait until they closed."""
        now = monotonic()
        for key, session in list(self.resting.items()):
            if now - session.rested_at > (_PARKED_TTL_SECONDS if key[2] else _IDLE_TTL_SECONDS):
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


SESSIONS = _SessionRegistry()
close_at_process_shutdown(SESSIONS.close_all)
