"""What the primary knows of each terminal's shell input line, so typing into a terminal is judged as what it runs.

🔒 Approval rules judge an agent's typing as the shell command its Enter runs (``entered_command``): what the line
held before, typed by anyone, joined with the new keys. Every input the primary sends a terminal goes through a
``TerminalStream``, one send at a time per terminal (``TerminalLine.lock``), which records it here, and the shell's
prompt marks, read from any stream attached to the terminal, say when it is back at its prompt.

The line is unknown after keys that edit it, recall history or complete (every control key but Enter and Ctrl-C),
from an Enter until the shell's next prompt, since a command or a continuation reads what is typed meanwhile, at that
prompt when anything was typed before it, after a send that may not have arrived, and in a terminal the primary did
not see open. Ctrl-C at the prompt empties it again.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from dataclasses import dataclass

# The shell's prompt command prints this before each prompt: the last command's exit status, and the prompt's number,
# which lets a mark replayed with the scrollback count once (``terminal_launch``).
PROMPT_COMMAND = (
    '__mindroom_status=$?; __mindroom_prompt=$((__mindroom_prompt + 1)); printf "\\033]133;D;%s;mindroom=%s\\007" '
    '"$__mindroom_status" "$__mindroom_prompt"'
)
PROMPT_MARK_START = b"\x1b]133;D;"
_PROMPT_MARK = re.compile(re.escape(PROMPT_MARK_START) + rb"(\d{1,3});mindroom=(\d{1,18})\x07")
# Longer than any prompt mark, so an unfinished one waits for the frame that ends it.
_MAX_MARK_BYTES = 64
_INTERRUPT = "\x03"
# Keys that edit the line, recall history, complete or end input, so what it holds can't be read from the keys.
_EDITING = re.compile(r"[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_MAX_LINE_CHARS = 4096
_MAX_TERMINALS = 4096
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


def keys_to_text(keys: str) -> str:
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


def run_text(command: str) -> str:
    """Return what ``terminal_run`` types for ``command``: the command line and Enter."""
    return command.strip("\n") + "\r"


def entered_command(typed: str | None, text: str) -> list[dict[str, object]] | None:
    """Return the ``run_shell_command`` call that typing ``text`` onto a line holding ``typed`` amounts to.

    Its ``args`` is what the Enters in ``text`` run, as one script, the line's earlier text included: an empty list
    when ``text`` enters nothing, and None when the line or the keys can't tell. Ctrl-C first empties a known line;
    on an unknown one, such as while a command runs, it may reach a program that keeps the keys after it.
    """
    rest = text.lstrip(_INTERRUPT)
    if not rest:
        return []
    if typed is None or _EDITING.search(rest):
        return None
    if rest != text:
        typed = ""
    end = max(rest.rfind("\r"), rest.rfind("\n"))
    if end == -1:
        return []
    return [{"args": (typed + rest[:end]).replace("\r", "\n")}]


@dataclass(frozen=True)
class LineState:
    """A terminal's line as one call was judged against: how often it changed, and its text, None when unknown."""

    revision: int
    typed: str | None


class TerminalLine:
    """One terminal's input line, as the primary saw it typed, and its shell's prompts."""

    def __init__(self, *, opened: bool) -> None:
        self.revision = 0
        self.typed: str | None = "" if opened else None
        # An Enter went in since the last prompt, so a command or a continuation reads what is typed now.
        self._busy = not opened
        # Something typed while busy waits for the next prompt, whose line it then holds.
        self._ahead = not opened
        # The prompts the shell prints before it reads a command typed now: its first, and one after each Enter or
        # Ctrl-C at the prompt; a command typed onto the line ends at the prompt after them.
        self._owed = 1 if opened else 0
        self.prompts = 0
        # Held across each send, so input reaches the shell in the order it is recorded.
        self.lock = asyncio.Lock()

    def state(self) -> LineState:
        """Return the line as a call typing now is judged against."""
        return LineState(self.revision, None if self._busy else self.typed)

    def unchanged(self, judged: LineState) -> bool:
        """Return whether the line is still the one ``judged`` describes, the text it claims included."""
        return judged.revision == self.revision and judged.typed in (None, self.state().typed)

    @property
    def empty(self) -> bool:
        """Whether the shell waits at its prompt with nothing typed."""
        return not self._busy and self.typed == ""

    @property
    def awaited(self) -> int:
        """The number of the last prompt mark the shell prints before it reads a command typed now."""
        return self.prompts + self._owed

    def type(self, text: str) -> None:
        """Record ``text`` as sent to the terminal."""
        self.revision += 1
        for char in text:
            if self._busy:
                # Ctrl-C interrupts the command, and the terminal drops the typing it has not read yet.
                self._ahead = char != _INTERRUPT
            elif char == _INTERRUPT:
                self.typed = ""
                self._owed += 1
            elif char in "\r\n":
                self._busy, self._ahead = True, False
                self._owed += 1
            elif self.typed is not None:
                self.typed = None if _EDITING.match(char) else self.typed + char
        if self.typed is not None and len(self.typed) > _MAX_LINE_CHARS:
            self.typed = None

    def lost(self) -> None:
        """Record a send that may or may not have arrived."""
        self.revision += 1
        self._busy = self._ahead = True

    def prompted(self, number: int) -> None:
        """Record the shell's prompt mark ``number``."""
        if number <= self.prompts:
            return
        self._owed = max(0, self._owed - (number - self.prompts))
        self.prompts = number
        # A prompt the shell owed from before the Enter, such as after a Ctrl-C, ends no command.
        if self._busy and not self._owed:
            self.typed = None if self._ahead else ""
            self._busy = self._ahead = False


class PromptMarks:
    """Finds the shell's prompt marks in one stream's output, a mark split across frames included."""

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, chunk: bytes) -> list[tuple[int, int]]:
        """Return the ``(number, status)`` of each mark that ``chunk`` completes."""
        data = self._tail + chunk
        start = data.rfind(b"\x1b")
        unfinished = start != -1 and len(data) - start < _MAX_MARK_BYTES and b"\x07" not in data[start:]
        self._tail = data[start:] if unfinished else b""
        return [(int(number), int(status)) for status, number in _PROMPT_MARK.findall(data)]


_lines: OrderedDict[tuple[object, str], TerminalLine] = OrderedDict()


def terminal_line(scope: object, session_id: str, *, opened: bool = False) -> TerminalLine:
    """Return the line of terminal ``session_id`` among the terminals ``scope`` names; ``opened`` for one just opened."""
    key = (scope, session_id)
    line = _lines.pop(key, None)
    if line is None:
        line = TerminalLine(opened=opened)
    _lines[key] = line
    while len(_lines) > _MAX_TERMINALS:
        _lines.popitem(last=False)
    return line


def line_state(scope: object, session_id: str) -> LineState | None:
    """Return the line of a terminal the primary has seen as a call typing now is judged against, else None."""
    line = _lines.get((scope, session_id))
    return None if line is None else line.state()


def forget_line(scope: object, session_id: str) -> None:
    """Forget a closed terminal's line."""
    _lines.pop((scope, session_id), None)
