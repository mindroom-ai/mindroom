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
from typing import TYPE_CHECKING

from mindroom.agent_cli.shell_contract import SHELL_FUNCTION
from mindroom.approval.tool_approval import tool_may_require_approval
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.constants import RuntimePaths  # noqa: TC001  # resolved by tool contract introspection
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.lines import keys_to_text, prompt_after
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,  # noqa: TC001  # resolved by tool contract introspection
)

if TYPE_CHECKING:
    from mindroom.terminal.client import TerminalStream, WorkerTerminals

_COLS, _ROWS = 120, 32
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 600.0
# How long a viewer waits for the scrollback, which the runner sends first, and for more output to read.
_SCROLLBACK_SECONDS = 1.0
# How long a viewer waits after the scrollback for the exit, which follows it at once for an ended shell.
_EXIT_SECONDS = 0.1
_OUTPUT_SECONDS = 0.5
# The most output one call keeps, its newest part, as much as the runner's scrollback; and the most text it answers.
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_TEXT_CHARS = 16_000
_ABOUT = (
    "Your terminals are persistent shells in your workspace, beside your shell commands, that people in the room can "
    "watch and type in from a chat client that shows terminals."
)
_READ_ONLY = (
    f"{_ABOUT} You can't type in them, because your run_shell_command calls may ask for approval: run commands with "
    "run_shell_command. terminal_read shows a terminal's recent output; terminal_list shows them all."
)
_NOT_AT_PROMPT = (
    "Error: the terminal's shell is not waiting at an empty prompt (a command is still running, or its line holds "
    "typing); nothing was typed. Read the terminal, send keys such as <ctrl-c> with terminal_send if needed, then run "
    "it again."
)
# Keys that edit the line, complete, interrupt or end input, which a command line never holds.
_KEYS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EXITED = "Error: the terminal's shell has exited; open another with terminal_open."
_DETACHED = "Error: the connection to the terminal ended; read it again before typing more."
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OTHER_ESCAPE = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?|\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_=>78]?")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BACKSPACED = re.compile(r"[^\x08\n]\x08")


def _plain_text(data: bytes) -> str:
    """Return terminal output as a person reads it on screen: escapes removed, carriage returns and backspaces applied."""
    text = data.decode("utf-8", errors="replace")
    for pattern in (_OSC, _CSI, _OTHER_ESCAPE):
        text = pattern.sub("", text)
    text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
    while (erased := _BACKSPACED.sub("", text)) != text:
        text = erased
    return _CONTROL.sub("", text)


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
                if after is not None and stream.prompt is not None and stream.prompt > after:
                    # Several prompts may arrive at once, as after a command someone typed while the run's ran.
                    found = prompt_after(bytes(kept), after)
                    if found is not None:
                        return bytes(kept[: found[0]]), cut, True, found[1]
    except TimeoutError:
        return bytes(kept), cut, False, None


def _refused(error: TerminalError, session_id: str | None = None) -> str:
    if error.status_code == 404 and session_id is not None:
        return f"Error: there is no terminal {session_id!r}; terminal_list shows yours."
    return f"Error: {error.detail}"


class TerminalTools(Toolkit):
    """Persistent terminals in the agent's workspace, which people in the room watch and type in."""

    name = "terminal"
    instructions = (
        f"{_ABOUT} Use them for work that keeps state between commands or that people should see: a dev server, a "
        "REPL, an interactive program, a long build. terminal_open starts one; terminal_run types one command line and "
        "waits for its prompt; terminal_send types keys such as <ctrl-c> or an answer to a prompt, and exit<enter> ends "
        "a terminal; terminal_read shows recent output; terminal_list shows them all."
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
        restricted = runtime_config is None or tool_may_require_approval(runtime_config, SHELL_FUNCTION)
        typing = () if restricted else (self.terminal_open, self.terminal_run, self.terminal_send)
        super().__init__(
            tools=(*typing, self.terminal_read, self.terminal_list) if self._terminals is not None else (),
            instructions=_READ_ONLY if restricted else None,
        )

    @property
    def _workspace_terminals(self) -> WorkerTerminals:
        assert self._terminals is not None
        return self._terminals

    async def terminal_open(self, cwd: str | None = None) -> str:
        """Open a new terminal in your workspace, which people in the room can watch.

        Args:
            cwd: Folder to start in, relative to your workspace; the workspace by default.

        Returns:
            The new terminal's session ID, or why it could not open.

        """
        try:
            info = await self._workspace_terminals.open(cols=_COLS, rows=_ROWS, cwd=cwd or None)
        except TerminalError as error:
            return _refused(error)
        return f"Opened terminal {info.id}."

    async def terminal_run(
        self,
        session_id: str,
        command: str,
        timeout_s: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        """Type one command line into a terminal, wait for its prompt, and return what it printed and its exit status.

        It types only while the shell waits at an empty prompt. A command still running at the timeout keeps running:
        read on with terminal_read, or stop it with terminal_send("<ctrl-c>"). Start servers and watchers here and
        leave them running; a terminal nobody types in or watches ends 30 minutes after its last input.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.
            command: One command line; join several with && or ;.
            timeout_s: Seconds to wait for the prompt, 30 by default.

        Returns:
            The command's output and exit status, or what it printed so far while it still runs.

        """
        command = command.strip("\n")
        # One line of text, so the run ends at the prompt after it.
        if _KEYS.search(command):
            return (
                "Error: a command is one line of plain text; join commands with && or ;, and type keys such as "
                "<ctrl-c> or <tab> with terminal_send."
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
            status = f"Still running after {timeout:g} s."
        elif exit_status is not None:
            status = f"Exit status {exit_status}."
        else:
            status = "The terminal's shell has exited."
        return f"{shown}\n[{status}]" if shown else f"[{status}]"

    async def terminal_send(self, session_id: str, keys: str) -> str:
        """Type keys into a terminal without waiting for a command: an answer to a prompt, or keys such as <ctrl-c>.

        Text is typed as written; <enter>, <tab>, <esc>, <backspace>, <up>, <down>, <left>, <right>, <home>, <end>,
        <delete> and <ctrl-c> (any <ctrl-letter>) are keys, so "y<enter>" answers a yes/no prompt. Send <ctrl-c> on its
        own: the terminal may drop keys typed right after it.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.
            keys: The text and keys to type.

        Returns:
            What the terminal printed right after, or why the keys were not typed.

        """
        text = keys_to_text(keys)
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
        exited = "\n[The terminal's shell has exited.]" if ended else ""
        return f"Sent. New output:\n{shown}{exited}" if shown else f"Sent.{exited}"

    async def terminal_read(self, session_id: str) -> str:
        """Read a terminal's recent output as text.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.

        Returns:
            Its recent output, and whether its shell has exited.

        """
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                output, _, exited, _ = await _output(stream, _OUTPUT_SECONDS)
        except TerminalError as error:
            return _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output).strip("\n")[-_MAX_TEXT_CHARS:]
        return f"{shown}\n[The terminal's shell has exited.]" if exited else shown or "[No output yet.]"

    async def terminal_list(self) -> str:
        """List the terminals of your workspace, including ones people opened.

        Returns:
            Each terminal's session ID and whether its shell still runs.

        """
        try:
            sessions = await self._workspace_terminals.sessions()
        except TerminalError as error:
            return _refused(error)
        if not sessions:
            return "No terminals are open. Open one with terminal_open."
        return "\n".join(
            f"- {info.id}: {'running' if info.alive else f'exited with status {info.exit_code}'}" for info in sessions
        )
