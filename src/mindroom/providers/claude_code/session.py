"""Own Claude Code starts, parked processes, and the process-wide concurrency cap."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    RateLimitEvent,
    SystemMessage,
)

from mindroom import __version__
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.background_tasks import create_background_task, wait_for_background_tasks
from mindroom.claude_sdk_process import ClaudeProcess, user_message
from mindroom.llm.errors import AuthenticationFailed, ProviderError, ProviderUnavailable
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.providers.claude_code.bridge import SERVER_NAME, TOOL_PREFIX, ToolBridge, tool_names
from mindroom.runtime_shutdown import close_at_process_shutdown

if TYPE_CHECKING:
    from collections.abc import Sequence

    from claude_agent_sdk import ClaudeSDKClient, RateLimitInfo

    from mindroom.llm.provider import ToolSpec
    from mindroom.providers.claude_code.prompt import Answer

__all__ = ["NOT_SIGNED_IN", "SESSIONS", "LiveSession"]

logger = get_logger(__name__)

# The oldest binary this route was run against; before 2.1.295 the model could reach tools beyond ``--tools``.
_MIN_VERSION = (2, 1, 328)
_PARKED_TTL_SECONDS = 30 * 60.0
_IDLE_TTL_SECONDS = 5 * 60.0
# A process takes a few hundred megabytes. One that answers a request is never closed to make room for another.
_MAX_PROCESSES = 8
_SWEEP_INTERVAL_SECONDS = 60.0
# How long Claude Code may take to say that it is ready, with MindRoom's tool server connected.
_READY_SECONDS = 30.0
# A tool result that holds images comes back from Claude Code as one message.
_MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# Claude Code features that would duplicate MindRoom's or hide its tools: memory files, compaction, and tool search.
_ISOLATION_ENV = {
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTO_COMPACT": "1",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # The SDK only defaults this when it did not inherit a value, so set it explicitly.
    "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
    # Give the SDK and CLI the default explicitly so the SDK need not read the parent environment.
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "600000",
    # A call that waits long is not moved to the background, where the model would go on without its result.
    "CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND": "1",
    "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
    # MindRoom already bounds its tool results; Claude Code would replace a longer one with a file it cannot read.
    "MAX_MCP_OUTPUT_TOKENS": "200000",
    # A parked call waits for MindRoom's loop, such as for an approval: Claude Code must outwait the parked process.
    "MCP_TOOL_TIMEOUT": str(int((_PARKED_TTL_SECONDS + 10 * _SWEEP_INTERVAL_SECONDS) * 1000)),
}
# Claude Code reads its sign-in, where it sends requests, and what it adds to them from variables named like these.
_SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
_SCRUB_NAMES = {"CLAUDE_BG_AUTH_SNAPSHOT_PATH", "CLAUDE_AUTO_BACKGROUND_TASKS"}
# The SDK reads these in the parent during connect, with no options override.
_SDK_PARENT_ENV = {"CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"}
# Fail safe: only these trusted names survive the scrub, even if the SDK adds other prefixed settings.
_SET_FOR_THE_PROCESS = {"CLAUDE_CODE_ENTRYPOINT", *_ISOLATION_ENV}
_CLI_ENV = "MINDROOM_CLAUDE_CODE_CLI"
_LAUNCHER_LOCK = Lock()
_LAUNCHER = f"""\
import os
import signal
import sys

real = os.environ.pop({_CLI_ENV!r})
keep = frozenset({tuple(sorted(_SET_FOR_THE_PROCESS))!r})
for name in tuple(os.environ):
    if name not in keep and (name.startswith({_SCRUB_PREFIXES!r}) or name in {tuple(sorted(_SCRUB_NAMES))!r}):
        del os.environ[name]
# Undo Python's ignored signals, which would otherwise survive exec.
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
os.execv(real, [real, *sys.argv[1:]])
"""
# How Claude Code names the plans of a Claude subscription; an account without one is "Claude API".
_PLAN = re.compile(r"Claude (Pro|Team|Enterprise|Max( \d+x)?)")
_SIGN_IN_HINT = "Run `claude auth login` as the user that runs MindRoom."
NOT_SIGNED_IN = f"Claude Code is not signed in. {_SIGN_IN_HINT}"
_NOT_FOUND = (
    "Claude Code was not found. Install it (https://claude.com/claude-code), run `claude auth login` as the user "
    "that runs MindRoom, and set `extra_kwargs.cli_path` if `claude` is not on MindRoom's PATH."
)
_OTHER_TOOLS = "Claude Code did not connect exactly MindRoom's tools, so it got no request."


@dataclass(eq=False)
class LiveSession:
    """One Claude Code process and what it last answered with.

    ``inventoried`` says the turn it runs started with exactly MindRoom's tools, before which none of its output
    counts. ``counted`` sums the usage its model messages reported in that turn, ``ledger`` what Claude Code said the
    process had used by the end of its last turn, and ``rejected`` is the plan limit it last reported.
    """

    process: ClaudeProcess
    bridge: ToolBridge
    provider: str
    inventoried: bool = False
    counted: dict[str, int] = field(default_factory=dict)
    ledger: dict[str, int] = field(default_factory=dict)
    rejected: RateLimitInfo | None = None
    answer: Answer | None = None
    rested_at: float = 0.0

    def broken(self) -> str | None:
        """Return why the resting process cannot be continued: its tool server failed, or it did not rest."""
        bridge = self.bridge
        messages = self.process.messages
        while bridge.failure is None and not messages.empty():
            message = messages.get_nowait()
            if isinstance(message, RateLimitEvent):
                self.rejected = message.rate_limit_info
            elif not isinstance(message, SystemMessage):
                bridge.failure = f"Claude Code did not wait for MindRoom ({type(message).__name__})"
        if bridge.failure is None and not all(call.made.done() for call in bridge.calls.values()):
            bridge.failure = "Claude Code made a tool call the model did not write"
        return bridge.failure

    async def send(self, blocks: list[dict[str, Any]]) -> None:
        """Send one user message, which starts a turn."""
        self.inventoried, self.counted = False, {}
        try:
            await self.process.client.query(user_message(blocks))
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
            f"Claude Code would use a credential from {other} instead of your Claude sign-in. Remove it and "
            "run `claude auth login`, or use `provider: anthropic` to pay with a key."
        )
    elif account.get("apiProvider") != "firstParty" or not _PLAN.fullmatch(str(account.get("subscriptionType"))):
        message = f"Claude Code is not signed in to a Claude subscription. {_SIGN_IN_HINT}"
    else:
        return None
    return AuthenticationFailed(message, provider=provider, status_code=401)


async def _refuse_unless_ready(client: ClaudeSDKClient, bridge: ToolBridge, provider: str, cli: Path) -> None:
    """Raise unless Claude Code may get a prompt: signed in to a subscription, with exactly the tools MindRoom offers.

    Its tool server connects a moment after it starts, so that is awaited.
    """
    info = await client.get_server_info()
    # The rest of what Claude Code says at start-up names the account, which is never logged.
    version = (info or {}).get("claude_code_version")
    logger.debug("claude_code_started", cli=str(cli), version=version)
    if (refused := _startup_error(info, provider)) is not None:
        raise refused
    wanted = bridge.expected_inventory()
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


def _prepare(root: Path, system: str, provider: str, cli_path: str | None) -> tuple[Path, Path | None, Path, Path]:
    """Resolve the CLI and prepare its private launcher, empty working directory, and optional prompt file."""
    cli = _resolve_cli(cli_path, provider)
    home = root / "claude_code"
    cwd = home / "cwd"
    for directory in (home, cwd):
        if directory.is_symlink():
            msg = f"Claude Code runtime directory is a symlink: {directory}"
            raise ProviderError(msg, provider=provider)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    if any(cwd.iterdir()):
        msg = f"Claude Code working directory is not empty: {cwd}"
        raise ProviderError(msg, provider=provider)
    launcher = _prepare_launcher(home, provider)
    if not system:
        return cwd, None, launcher, cli
    # A system prompt is too long for a command line.
    descriptor, path = tempfile.mkstemp(dir=home)
    prompt = Path(path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(system)
    except BaseException:
        with suppress(OSError):
            prompt.unlink(missing_ok=True)
        raise
    return cwd, prompt, launcher, cli


def _resolve_cli(cli_path: str | None, provider: str) -> Path:
    """Resolve the configured path or command, or the `claude` the builder found on `PATH`."""
    if cli_path and os.sep not in cli_path:
        cli_path = shutil.which(cli_path)
    if cli_path:
        cli = Path(cli_path).absolute()
        if cli.is_file() and os.access(cli, os.X_OK):
            return cli
    raise AuthenticationFailed(_NOT_FOUND, provider=provider)


def _prepare_launcher(home: Path, provider: str) -> Path:
    """Publish a stable executable without rewriting the file another start may be executing."""
    launcher = (home / "exec-claude").absolute()
    payload = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -IS -c {shlex.quote(_LAUNCHER)} "$@"\n'.encode()
    with _LAUNCHER_LOCK:
        if (
            not launcher.is_file(follow_symlinks=False)
            or stat.S_IMODE(launcher.lstat().st_mode) != 0o700
            or launcher.read_bytes() != payload
        ):
            with open_directory_within_root(home) as directory:
                atomic_write_bytes_at(directory, launcher.name, payload, file_mode=0o700)
    if not os.access(launcher, os.X_OK):
        msg = f"Claude Code launcher is not executable: {launcher}. Check its permissions and the storage mount."
        raise ProviderUnavailable(msg, provider=provider)
    return launcher


type _RestKey = tuple[str, str, tuple[str, ...]]


class _SessionRegistry:
    """Every Claude Code process of this runtime, and the resting ones by the request each answered.

    A resting process is kept under its conversation, the request it answered, and the calls it waits on.
    A process holds a slot until it is gone: ``starting`` counts the starts without one yet, ``live`` holds the rest.
    """

    def __init__(self) -> None:
        self.live: set[LiveSession] = set()
        self.resting: dict[_RestKey, LiveSession] = {}
        self.starting = 0
        self._sweep: asyncio.Task[None] | None = None

    async def start(
        self,
        *,
        provider: str,
        storage_root: Path,
        system: str = "",
        tools: Sequence[ToolSpec] = (),
        replaces: LiveSession | None = None,
        **options: Any,  # noqa: ANN401 - fields of ``ClaudeAgentOptions``
    ) -> LiveSession:
        """Start and verify a process, reusing its predecessor's slot when replacing it.

        Cancellation cannot cut short startup: only at its end do we know whether the process holds the slot.
        """
        if names := sorted(_SDK_PARENT_ENV.intersection(os.environ)):
            msg = f"Unset {', '.join(names)} in MindRoom's environment. The Claude Agent SDK reads these directly."
            raise AuthenticationFailed(msg, provider=provider)
        freed = self._reserve(provider, replaces)
        starting = create_background_task(
            self._launch(freed, provider, storage_root, system, tools, options),
            name="claude_code_start",
            owner=self,
            context=Context(),
            log_exceptions=False,  # The waiting request raises its failure.
        )
        try:
            return await asyncio.shield(starting)
        except asyncio.CancelledError:
            # Nothing waits for the process any more, so it closes once its start ended.
            starting.add_done_callback(lambda done: done.cancelled() or done.exception() or self.retire(done.result()))
            raise

    async def _launch(
        self,
        freed: asyncio.Task[None] | None,
        provider: str,
        storage_root: Path,
        system: str,
        tools: Sequence[ToolSpec],
        options: dict[str, Any],
    ) -> LiveSession:
        """Start one process in the slot ``start`` took, once ``freed`` ended, and give the slot up if that fails."""
        session = prompt = None
        try:
            if freed is not None:
                await freed
            bridge = ToolBridge(names=tool_names(tools, provider))
            cwd, prompt, launcher, cli = await asyncio.to_thread(
                _prepare,
                storage_root,
                system,
                provider,
                options.get("cli_path"),
            )
            sdk_options = ClaudeAgentOptions(
                **options,
                system_prompt={"type": "file", "path": str(prompt)} if prompt else "",
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
                env={**_ISOLATION_ENV, _CLI_ENV: str(cli)},
                extra_args={"no-session-persistence": None},
                max_buffer_size=_MAX_MESSAGE_BYTES,
                client_app=f"mindroom/{__version__}",
                stderr=lambda line: logger.debug("claude_code_stderr", line=line),
            )
            sdk_options.cli_path = launcher
            if tools:
                server = bridge.server(tools)
                sdk_options.mcp_servers = {SERVER_NAME: {"type": "sdk", "name": SERVER_NAME, "instance": server}}
                sdk_options.allowed_tools = [f"{TOOL_PREFIX}*"]
            process = await ClaudeProcess.open(sdk_options, messages=bridge.messages)
            session = LiveSession(process, bridge, provider)
            self.starting -= 1
            self.live.add(session)
            async with asyncio.timeout(_READY_SECONDS):
                await _refuse_unless_ready(process.client, bridge, provider, cli)
        except BaseException as error:
            if session is None:
                self.starting -= 1
            else:
                await self.retire(session)
            if isinstance(error, CLINotFoundError):
                raise AuthenticationFailed(_NOT_FOUND, provider=provider) from error
            # The SDK raises a plain exception for a request about its state that Claude Code rejects.
            if isinstance(error, Exception) and not isinstance(error, ProviderError):
                msg = f"Claude Code did not start: {error or type(error).__name__}"
                raise ProviderUnavailable(msg, provider=provider) from error
            raise
        finally:
            # Claude Code read the prompt when it started.
            if prompt is not None:
                await asyncio.to_thread(prompt.unlink, missing_ok=True)
        return session

    def _reserve(self, provider: str, replaced: LiveSession | None) -> asyncio.Task[None] | None:
        """Take the slot of a new process, and return the end of the process whose slot it is, which the start awaits.

        That is ``replaced``, or at the limit a resting one this closes, an idle one before a parked one and the
        longest resting first. With every process answering a request, the new one is refused.
        """
        if replaced not in self.live:
            replaced = None  # A process that is gone holds no slot to take.
        if replaced is None and len(self.live) + self.starting >= _MAX_PROCESSES:
            if not self.resting:
                msg = f"MindRoom already runs its limit of {_MAX_PROCESSES} Claude Code processes. Try again shortly."
                raise ProviderUnavailable(msg, provider=provider)
            key = min(self.resting, key=lambda key: (bool(key[2]), self.resting[key].rested_at))
            replaced = self.resting.pop(key)
        self.starting += 1
        return None if replaced is None else self.retire(replaced)

    def claim(self, key: _RestKey) -> LiveSession | None:
        """Take a resting process out of circulation, so no other request can continue it."""
        return self.resting.pop(key, None)

    def retire(self, session: LiveSession) -> asyncio.Task[None]:
        """Close ``session`` on a task no stopped request cuts short; it holds its slot until it is gone."""

        async def run() -> None:
            try:
                await session.process.close()
            finally:
                self.live.discard(session)

        return create_background_task(run(), name="claude_code_close", owner=self, context=Context())

    def rest(self, key: _RestKey, session: LiveSession) -> None:
        """Keep ``session`` for the request that follows the answer it just gave."""
        if (replaced := self.resting.pop(key, None)) is not None:
            self.retire(replaced)
        session.rested_at = monotonic()
        self.resting[key] = session
        if self._sweep is None or self._sweep.done():
            self._sweep = create_background_task(self._sweep_expired(), name="claude_code_sweep", context=Context())

    def superseded(self, conversation: str, call_ids: set[str]) -> list[LiveSession]:
        """Take what a request that starts over supersedes: an idle process, or one that waits on ``call_ids``."""
        keys = [key for key in self.resting if key[0] == conversation and (not key[2] or call_ids.intersection(key[2]))]
        return [self.resting.pop(key) for key in keys]

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
