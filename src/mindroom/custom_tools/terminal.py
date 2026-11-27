"""An agent's persistent terminals: open one, run a command in it, type keys, read it, and list them.

They are the terminals people see for the agent's workspace as this turn resolves it (``agent_terminals``), in the
worker where its shell commands run, and each function types and reads as a viewer attached for the call. An agent
whose shell runs in the primary process, or offers no ``run_shell_command``, has none and is offered no function.

🔒 An agent types into them only while its ``run_shell_command`` calls never ask for approval: what text typed into a
persistent shell runs depends on what ran there before (aliases, functions, a program reading the keys, history, shell
options), so no rule can judge it from the text. Any other agent is offered only reading and listing, so no path can
call a typing function and no approval card can ask for one.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.constants import RuntimePaths  # noqa: TC001  # resolved by tool contract introspection
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.lines import prompt_after
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,  # noqa: TC001  # resolved by tool contract introspection
)
from mindroom.tools.native_helpers import validate_action_params

if TYPE_CHECKING:
    from mindroom.terminal.client import TerminalStream, WorkerTerminals
    from mindroom.tool_system.tool_function import ToolFunction

_COLS, _ROWS = 120, 32
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 600.0
# How long a viewer waits for the scrollback, which the runner sends first, and for more output to read.
_SCROLLBACK_SECONDS = 1.0
_OUTPUT_SECONDS = 0.5
# How long a viewer waits after the scrollback for the exit, which follows it at once for an ended shell.
_EXIT_SECONDS = 0.1
# The most output one call keeps, its newest part, as much as the runner's scrollback; and the most text it answers.
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_TEXT_CHARS = 16_000
_ABOUT = "Persistent shells in your workspace that people in the room can watch (and type in where config allows)."
_ReadAction = Literal["list", "read"]
_WriteAction = Literal["open", "run", "send"]
_READ_ALLOWED: dict[str, frozenset[str]] = {
    "list": frozenset(),
    "read": frozenset({"session_id"}),
}
_WRITE_ALLOWED: dict[str, frozenset[str]] = {
    "open": frozenset({"cwd"}),
    "run": frozenset({"session_id", "command", "timeout_s"}),
    "send": frozenset({"session_id", "keys"}),
}
_READ_ONLY = (
    f"{_ABOUT} You can't type because run_shell_command may ask for approval: run commands with "
    "run_shell_command. Use terminal_read (`list`, `read`) to inspect terminals."
)
_NOT_AT_PROMPT = (
    "Error: the terminal's shell is not waiting at an empty prompt (a command is still running, its line holds typing, "
    "or keys typed while a command ran may be queued); nothing was typed. To keep a running command, run this in "
    'another terminal from terminal_write(action="open"); otherwise send <ctrl-c> with terminal_write(action="send"), '
    "which an idle prompt needs too after such keys, then run it again."
)
# Keys that edit the line, complete, interrupt or end input, which a command line never holds.
_KEYS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EXITED = 'Error: the terminal\'s shell has exited; open another with terminal_write(action="open").'
_SHELL_EXITED = "[The terminal's shell has exited.]"
_DETACHED = "Error: the connection to the terminal ended; read it again before typing more."
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OTHER_ESCAPE = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?|\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_=>78]?")
_NAMED_KEYS = {
    "enter": "\r",
    "tab": "\t",
    "esc": "\x1b",
    "backspace": "\x7f",
    "space": " ",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "delete": "\x1b[3~",
}
_KEY = re.compile(r"<([A-Za-z-]+)>")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(data: bytes) -> str:
    """Return terminal output as a person reads it: escapes removed, carriage returns and backspaces applied."""
    text = data.decode("utf-8", errors="replace")
    for pattern in (_OSC, _CSI, _OTHER_ESCAPE):
        text = pattern.sub("", text)
    lines = (_erased(line.rstrip("\r").rsplit("\r", 1)[-1]) for line in text.split("\n"))
    return _CONTROL.sub("", "\n".join(lines))


def _erased(line: str) -> str:
    """Return ``line`` with each backspace and the character before it removed, in one pass."""
    if "\x08" not in line:
        return line
    kept: list[str] = []
    for char in line:
        if char != "\x08":
            kept.append(char)
        elif kept:
            kept.pop()
    return "".join(kept)


def _keys_to_text(keys: str) -> str:
    """Return what typing ``keys`` sends: text as written, with ``<enter>``, ``<up>``, ``<ctrl-c>`` and the like as keys."""

    def key(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if name in _NAMED_KEYS:
            return _NAMED_KEYS[name]
        letter = name.removeprefix("ctrl-")
        if letter != name and len(letter) == 1 and letter.isalpha():
            return chr(ord(letter) - ord("a") + 1)
        return match.group(0)

    return _KEY.sub(key, keys)


async def _ended(stream: TerminalStream) -> bool:
    """Read what the runner sends a viewer first, the scrollback and an ended shell's exit; return whether it ended.

    Its prompt marks so count before anything is typed.
    """
    with contextlib.suppress(TimeoutError):
        for seconds in (_SCROLLBACK_SECONDS, _EXIT_SECONDS):
            async with asyncio.timeout(seconds):
                if (await stream.receive()).type == "exit":
                    return True
    return False


async def _output(
    stream: TerminalStream,
    seconds: float,
    *,
    after: int | None = None,
) -> tuple[bytes, bool, bool, int | None]:
    """Return what ``stream`` prints within ``seconds``, whether older output was dropped, whether it ended, and how.

    It keeps the newest ``_MAX_OUTPUT_BYTES``, and ends at the shell's exit or, given ``after``, at the first prompt
    mark numbered above it, before which it cuts the output, with the exit status that mark reports. A lost connection
    raises ``TerminalDetachedError``.
    """
    kept = bytearray()
    cut = False
    try:
        async with asyncio.timeout(seconds):
            while True:
                event = await stream.receive()
                if event.type == "exit":
                    return bytes(kept), cut, True, None
                kept += event.data
                if len(kept) > _MAX_OUTPUT_BYTES:
                    del kept[: len(kept) - _MAX_OUTPUT_BYTES]
                    cut = True
                if after is not None and stream.prompt > after:
                    # Several prompts may arrive at once, as after a command someone typed while the run's command ran.
                    found = prompt_after(bytes(kept), after)
                    if found is not None:
                        return bytes(kept[: found[0]]), cut, True, found[1]
    except TimeoutError:
        return bytes(kept), cut, False, None


def _refused(error: TerminalError, session_id: str | None = None) -> str:
    if error.status_code == 404 and session_id is not None:
        return f'Error: there is no terminal {session_id!r}; terminal_read(action="list") shows yours.'
    return f"Error: {error.detail}"


class TerminalTools(Toolkit):
    """Persistent terminals in the agent's workspace, which people in the room watch and type in where its config allows."""

    name = "terminal"
    instructions = (
        f"{_ABOUT} Use for stateful or visible work (dev servers, REPLs, interactive tools, long builds). "
        "terminal_write: `open` starts one, `run` runs one command line, `send` types keys (e.g. <ctrl-c>, exit<enter>). "
        "terminal_read: `read` shows recent output, `list` lists all."
    )

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        runtime_config: Config | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        identity = None if worker_target is None else worker_target.execution_identity
        agent_name = None if worker_target is None else worker_target.routing_agent_name
        self._terminals = (
            None
            if runtime_config is None or identity is None or agent_name is None
            else agent_terminals(agent_name, runtime_config, runtime_paths, identity, create=True)
        )
        self._restricted = runtime_config is None or tool_may_require_approval(
            runtime_config,
            SHELL_FUNCTION,
            agent_name=agent_name,
        )
        offered = (self.terminal_read,) if self._restricted else (self.terminal_write, self.terminal_read)
        super().__init__(
            tools=offered if self._terminals is not None else (),
            instructions=_READ_ONLY if self._restricted else None,
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Omit `background_after` on fast terminal reads while keeping output-file redirection."""
        if function.name == "terminal_read":
            return replace(function, offer_background_after=False)
        return function

    @property
    def _workspace_terminals(self) -> WorkerTerminals:
        assert self._terminals is not None
        return self._terminals

    async def terminal_write(
        self,
        action: _WriteAction,
        session_id: str | None = None,
        command: str | None = None,
        keys: str | None = None,
        cwd: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        """Open a terminal, run one command line, or type keys.

        - `open`: start a workspace terminal.
        - `run`: run one command line at an empty prompt; if still running at timeout, read with `terminal_read(action="read")` or stop with `<ctrl-c>` (running commands stay alive; idle terminals end after 30m).
        - `send`: type verbatim text/keys (`<enter|tab|esc|backspace|space|up|down|left|right|home|end|delete|ctrl-letter>`) without waiting; send `<ctrl-c>` alone.

        Args:
            action: Action.
            session_id: Terminal ID (run, send).
            command: One command line; join with && or ; (run).
            keys: Verbatim text/keys (send).
            cwd: Workspace-relative dir (open).
            timeout_s: Wait seconds (run, default 30, max 600).

        """
        params: dict[str, object] = {
            "session_id": session_id,
            "command": command,
            "keys": keys,
            "cwd": cwd,
            "timeout_s": timeout_s,
        }
        if (error := validate_action_params("terminal_write", action, _WRITE_ALLOWED, params)) is not None:
            return f"Error: {error}"
        if action == "open":
            return await self._open_terminal(cwd=cwd)
        if not isinstance(session_id, str) or not session_id.strip():
            return f"Error: session_id is required for action={action!r}."
        resolved_session_id = session_id.strip()
        if action == "run":
            return await self._run_command(
                resolved_session_id,
                command,
                timeout_s=_DEFAULT_TIMEOUT_SECONDS if timeout_s is None else timeout_s,
            )
        return await self._send_keys(resolved_session_id, keys)

    async def terminal_read(
        self,
        action: _ReadAction,
        session_id: str | None = None,
    ) -> str:
        """Inspect workspace terminals.

        - `list`: list terminals, including ones people opened.
        - `read`: recent output and exit state.

        Args:
            action: Action.
            session_id: Terminal ID (read).

        """
        if (
            error := validate_action_params("terminal_read", action, _READ_ALLOWED, {"session_id": session_id})
        ) is not None:
            return f"Error: {error}"
        if action == "list":
            return await self._list_terminals()
        if not isinstance(session_id, str) or not session_id.strip():
            return "Error: session_id is required for action='read'."
        return await self._read_terminal(session_id.strip())

    async def _open_terminal(self, *, cwd: str | None = None) -> str:
        try:
            info = await self._workspace_terminals.open(cols=_COLS, rows=_ROWS, cwd=cwd or None)
        except TerminalError as error:
            return _refused(error)
        return f"Opened terminal {info.id}."

    async def _run_command(
        self,
        session_id: str,
        command: str | None,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        if not isinstance(command, str) or not command.strip():
            return "Error: command is required for action='run'."
        command = command.strip("\n")
        # One line of text, so the run ends at the prompt after it.
        if _KEYS.search(command):
            return (
                "Error: a command is one line of plain text; join commands with && or ;, and type keys such as "
                '<ctrl-c> or <tab> with terminal_write(action="send").'
            )
        timeout = max(0.1, min(float(timeout_s), _MAX_TIMEOUT_SECONDS))
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                if await _ended(stream):
                    return _EXITED
                before = await stream.send_input(f"{command}\r", empty_line=True)
                output, cut, finished, exit_status = await _output(stream, timeout, after=before)
        except TerminalError as error:
            return _NOT_AT_PROMPT if error.status_code == 409 else _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output)
        # The first line is the shell's echo of the command, unless it was dropped with older output.
        shown = (shown if cut else shown.partition("\n")[2]).strip("\n")[-_MAX_TEXT_CHARS:]
        if not finished:
            status = f"[Still running after {timeout:g} s.]"
        elif exit_status is not None:
            status = f"[Exit status {exit_status}.]"
        else:
            status = _SHELL_EXITED
        return f"{shown}\n{status}" if shown else status

    async def _send_keys(self, session_id: str, keys: str | None) -> str:
        if not isinstance(keys, str) or keys == "":
            return "Error: keys is required for action='send'."
        text = _keys_to_text(keys)
        if "\x03" in text.rstrip("\x03"):
            return "Error: send <ctrl-c> on its own, then the keys after it; the terminal may drop keys typed with it."
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                if await _ended(stream):
                    return _EXITED
                await stream.send_input(text)
                output, _, ended, _ = await _output(stream, _OUTPUT_SECONDS)
        except TerminalError as error:
            return _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output).strip()[-_MAX_TEXT_CHARS:]
        exited = f"\n{_SHELL_EXITED}" if ended else ""
        return f"Sent. New output:\n{shown}{exited}" if shown else f"Sent.{exited}"

    async def _read_terminal(self, session_id: str) -> str:
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                output, _, exited, _ = await _output(stream, _OUTPUT_SECONDS)
        except TerminalError as error:
            return _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output).strip("\n")[-_MAX_TEXT_CHARS:]
        return f"{shown}\n{_SHELL_EXITED}" if exited else shown or "[No output yet.]"

    async def _list_terminals(self) -> str:
        try:
            sessions = await self._workspace_terminals.sessions()
        except TerminalError as error:
            return _refused(error)
        if not sessions:
            return (
                "No terminals are open."
                if self._restricted
                else 'No terminals are open. Open one with terminal_write(action="open").'
            )
        return "\n".join(
            f"- {info.id}: {'running' if info.alive else f'exited with status {info.exit_code}'}" for info in sessions
        )
