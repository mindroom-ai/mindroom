"""An agent's persistent terminals: open one, run a command in it, type keys, read it, and list them.

They are the terminals people see for the agent's workspace as this turn resolves it (``agent_terminals``), in the
worker where its shell commands run, and each function types and reads as a viewer attached for the call. An agent
whose shell runs in the primary process, or offers no ``run_shell_command``, has none and is offered no function.
How the shell's approval rules judge the typing is in ``mindroom.tools.terminal``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.constants import RuntimePaths  # noqa: TC001  # resolved by tool contract introspection
from mindroom.terminal.client import TerminalDetachedError
from mindroom.terminal.lines import (
    PROMPT_MARK_START,
    LineState,
    entered_command,
    keys_to_text,
    line_state,
    run_text,
)
from mindroom.terminal.resolution import agent_terminals
from mindroom.terminal.session import TerminalError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_schema import closed_parameters
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,  # noqa: TC001  # resolved by tool contract introspection
)

if TYPE_CHECKING:
    from typing import Any

    from mindroom.terminal.client import TerminalStream, WorkerTerminals
    from mindroom.tool_system.tool_function import ToolFunction

_COLS, _ROWS = 120, 32
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 600.0
# How long a viewer waits for the scrollback, which the runner sends first, and for more output to read.
_SCROLLBACK_SECONDS = 1.0
_OUTPUT_SECONDS = 0.5
# The most output one call keeps, its newest part, as much as the runner's scrollback; and the most text it answers.
_MAX_OUTPUT_BYTES = 256 * 1024
_MAX_TEXT_CHARS = 16_000
_LINE_CHANGED = (
    "Error: the terminal's line changed after these keys were checked (someone or another call typed in it); nothing "
    "was typed. Read the terminal, then send them again."
)
_NOT_AT_PROMPT = (
    "Error: the terminal's shell is not waiting at an empty prompt (a command is still running, or its line holds "
    "typing); nothing was typed. Read the terminal, and send keys such as <ctrl-c> with terminal_send."
)
_DETACHED = "Error: the connection to the terminal ended, and keys sent may or may not have arrived. Read it again."
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OTHER_ESCAPE = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?|\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_=>78]?")
_CONTROL = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")


def _plain_text(data: bytes) -> str:
    """Return terminal output as the text a person reads on screen: escapes removed, carriage returns applied."""
    text = data.decode("utf-8", errors="replace")
    for pattern in (_OSC, _CSI, _OTHER_ESCAPE):
        text = pattern.sub("", text)
    lines = [line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n")]
    return _CONTROL.sub("", "\n".join(lines))


async def _scrollback(stream: TerminalStream) -> None:
    """Read the scrollback, which the runner sends a viewer first, so its prompt marks count before anything is typed."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_SCROLLBACK_SECONDS):
            await stream.receive()


async def _output(
    stream: TerminalStream,
    seconds: float,
    *,
    prompt_after: int | None = None,
) -> tuple[bytes, bool, bool]:
    """Return what ``stream`` prints within ``seconds``, whether older output was dropped, and whether it ended.

    It keeps the newest ``_MAX_OUTPUT_BYTES``, and ends at the shell's exit or, given ``prompt_after``, at the first
    prompt mark numbered above it, before which it cuts the output. A lost connection raises ``TerminalDetachedError``.
    """
    kept = bytearray()
    cut = False
    try:
        async with asyncio.timeout(seconds):
            while True:
                event = await stream.receive()
                if event.type == "exit":
                    return bytes(kept), cut, True
                kept += event.data
                if len(kept) > _MAX_OUTPUT_BYTES:
                    del kept[: len(kept) - _MAX_OUTPUT_BYTES]
                    cut = True
                if prompt_after is not None and stream.prompt is not None and stream.prompt[0] > prompt_after:
                    return bytes(kept).rsplit(PROMPT_MARK_START, 1)[0], cut, True
    except TimeoutError:
        return bytes(kept), cut, False


def _refused(error: TerminalError, session_id: str | None = None) -> str:
    if error.status_code == 404 and session_id is not None:
        return f"Error: there is no terminal {session_id!r}; terminal_list shows yours."
    return f"Error: {error.detail}"


class TerminalTools(Toolkit):
    """Persistent terminals in the agent's workspace, which people in the room watch and type in."""

    name = "terminal"
    instructions = (
        "Your terminals are persistent shells in your workspace, beside your shell commands, that people in the room "
        "can watch and type in from a chat client that shows terminals. Use them for work that keeps state between "
        "commands or that people should see: a dev server, a REPL, an interactive program, a long build. "
        "terminal_open starts one; terminal_run types one command line and waits for its prompt; terminal_send types "
        "keys such as <ctrl-c> or an answer to a prompt, and exit<enter> ends a terminal; terminal_read shows recent "
        "output; terminal_list shows them all."
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
        functions = (self.terminal_open, self.terminal_run, self.terminal_send, self.terminal_read, self.terminal_list)
        super().__init__(tools=functions if self._terminals is not None else ())

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Hide ``terminal_send``'s ``line`` from the model, which only preparation before its approval fills."""
        if function.name != "terminal_send":
            return function
        properties = {name: schema for name, schema in function.parameters["properties"].items() if name != "line"}
        return replace(
            function,
            parameters=closed_parameters(self.terminal_send, {**function.parameters, "properties": properties}),
            prepare_approval=self._with_line,
        )

    def _with_line(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return ``arguments`` with the terminal's line as the keys are judged and then typed against."""
        session_id = arguments.get("session_id")
        state = line_state(self._workspace_terminals.scope, session_id) if isinstance(session_id, str) else None
        return {**arguments, "line": None if state is None else asdict(state)}

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

    async def terminal_run(self, session_id: str, command: str, timeout_s: float = _DEFAULT_TIMEOUT_SECONDS) -> str:
        """Type one command line into a terminal, wait for its prompt, and return what it printed and its exit status.

        It types only while the shell waits at an empty prompt. A command still running at the timeout keeps running:
        read on with terminal_read, or stop it with terminal_send("<ctrl-c>"). Start servers and watchers here and
        leave them running.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.
            command: One command line; join several with && or ;.
            timeout_s: Seconds to wait for the prompt, 30 by default.

        Returns:
            The command's output and exit status, or what it printed so far while it still runs.

        """
        text = run_text(command)
        # One line, so the run ends at the prompt after it; text, so its judgment is what it runs.
        if "\n" in text or "\r" in text[:-1] or "\x03" in text or entered_command("", text) is None:
            return (
                "Error: a command is one line of plain text; join commands with && or ;, and type keys such as "
                "<ctrl-c> or <tab> with terminal_send."
            )
        timeout = max(0.1, min(float(timeout_s), _MAX_TIMEOUT_SECONDS))
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                await _scrollback(stream)
                before = await stream.send_input(text, empty_line=True)
                output, cut, finished = await _output(stream, timeout, prompt_after=before)
        except TerminalError as error:
            return _NOT_AT_PROMPT if error.status_code == 409 else _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output)
        # The first line is the shell's echo of the command, unless it was dropped with older output.
        shown = (shown if cut else shown.partition("\n")[2]).strip("\n")[-_MAX_TEXT_CHARS:]
        if not finished:
            status = f"Still running after {timeout:g} s."
        elif stream.prompt is not None and stream.prompt[0] > before:
            status = f"Exit status {stream.prompt[1]}."
        else:
            status = "The terminal's shell has exited."
        return f"{shown}\n[{status}]" if shown else f"[{status}]"

    async def terminal_send(self, session_id: str, keys: str, line: LineState | None = None) -> str:
        """Type keys into a terminal without waiting for a command: an answer to a prompt, or keys such as <ctrl-c>.

        Text is typed as written; <enter>, <tab>, <esc>, <backspace>, <up>, <down>, <left>, <right>, <home>, <end>,
        <delete> and <ctrl-c> (any <ctrl-letter>) are keys, so "y<enter>" answers a yes/no prompt.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.
            keys: The text and keys to type.
            line: Filled by MindRoom, never by the caller.

        Returns:
            What the terminal printed right after, or why the keys were not typed.

        """
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                await _scrollback(stream)
                await stream.send_input(keys_to_text(keys), judged=line)
                output, _, _ = await _output(stream, _OUTPUT_SECONDS)
        except TerminalError as error:
            return _LINE_CHANGED if error.status_code == 409 else _refused(error, session_id)
        except TerminalDetachedError:
            return _DETACHED
        shown = _plain_text(output).strip()[-_MAX_TEXT_CHARS:]
        return f"Sent. New output:\n{shown}" if shown else "Sent."

    async def terminal_read(self, session_id: str) -> str:
        """Read a terminal's recent output as text.

        Args:
            session_id: The terminal, from terminal_open or terminal_list.

        Returns:
            Its recent output, and its shell's exit status once it has exited.

        """
        try:
            async with self._workspace_terminals.attach(session_id) as stream:
                output, _, exited = await _output(stream, _OUTPUT_SECONDS)
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
