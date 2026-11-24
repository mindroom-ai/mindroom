"""What the primary knows of each terminal's shell input line, so ``terminal_run`` types only at an empty prompt.

Every input the primary sends a terminal goes through a ``TerminalStream``, one send at a time per terminal
(``TerminalLine.lock``), which records it here, and the shell's prompt marks, read from any stream attached to the
terminal, say when it is back at its prompt and which prompt ends a command typed now.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict

# The shell's prompt command (``terminal_launch``) prints this before each prompt: the last command's exit status, and
# the prompt's number, which lets a mark replayed with the scrollback count once. It keeps itself from the shells
# started inside, whose own prompts would count from 1 again; until such a shell exits, the line waits for a prompt.
PROMPT_COMMAND = (
    "__mindroom_status=$?; export -n PROMPT_COMMAND; __mindroom_prompt=$((__mindroom_prompt + 1)); "
    'printf "\\033]133;D;%s;mindroom=%s\\007" "$__mindroom_status" "$__mindroom_prompt"'
)
_PROMPT_MARK = re.compile(rb"\x1b\]133;D;(\d{1,3});mindroom=(\d{1,18})\x07")
# Longer than any prompt mark, so an unfinished one waits for the frame that ends it.
_MAX_MARK_BYTES = 64
_INTERRUPT = "\x03"
_MAX_TERMINALS = 4096


class TerminalLine:
    """One terminal's input line, as the primary saw it typed, and its shell's prompts."""

    def __init__(self, *, opened: bool) -> None:
        # An Enter or a Ctrl-C went in since the last prompt: a command or a continuation reads what is typed now, or
        # the interrupt may drop it, and typing that reaches the shell first may keep it from printing that prompt.
        self._busy = not opened
        # The line holds typing; while busy, typing that waits for the next prompt, whose line it then holds.
        self._held = not opened
        # An Enter typed while busy: the shell runs that line after the prompt, and stays busy until a Ctrl-C drops it.
        self._queued = not opened
        # The prompts the shell prints before it reads a command typed now: its first, and one after each Enter or
        # Ctrl-C at the prompt; a command typed onto the line ends at the prompt after them.
        self._owed = 1 if opened else 0
        self.prompts = 0
        # Held across each send, so input reaches the shell in the order it is recorded.
        self.lock = asyncio.Lock()

    @property
    def empty(self) -> bool:
        """Whether the shell waits at its prompt with nothing typed."""
        return not self._busy and not self._held

    @property
    def awaited(self) -> int:
        """The number of the last prompt mark the shell prints before it reads a command typed now."""
        return self.prompts + self._owed

    def type(self, text: str) -> None:
        """Record ``text`` as sent to the terminal."""
        for char in text:
            if self._busy:
                # Ctrl-C interrupts the command, and the terminal drops the typing it has not read yet.
                self._held = char != _INTERRUPT
                self._queued = (self._queued or char in "\r\n") and char != _INTERRUPT
            elif char in "\r\n" + _INTERRUPT:
                self._busy, self._held = True, False
                self._owed += 1
            else:
                self._held = True

    def lost(self) -> None:
        """Record a send that may or may not have arrived."""
        self._busy = self._held = self._queued = True

    def prompted(self, number: int) -> None:
        """Record the shell's prompt mark ``number``."""
        if number <= self.prompts:
            return
        self._owed = max(0, self._owed - (number - self.prompts))
        self.prompts = number
        # A prompt the shell owed from before the Enter, such as after a Ctrl-C, ends no command.
        if self._busy and not self._owed and not self._queued:
            self._busy = False


class PromptMarks:
    """Finds the shell's prompt marks in one stream's output, a mark split across frames included."""

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, chunk: bytes) -> list[int]:
        """Return the number of each mark that ``chunk`` completes."""
        data = self._tail + chunk
        start = data.rfind(b"\x1b")
        unfinished = start != -1 and len(data) - start < _MAX_MARK_BYTES and b"\x07" not in data[start:]
        self._tail = data[start:] if unfinished else b""
        return [int(number) for _, number in _PROMPT_MARK.findall(data)]


def prompt_after(data: bytes, number: int) -> tuple[int, int] | None:
    """Return where in ``data`` the first prompt mark numbered above ``number`` starts, and the exit status it reports."""
    for mark in _PROMPT_MARK.finditer(data):
        if int(mark[2]) > number:
            return mark.start(), int(mark[1])
    return None


_lines: OrderedDict[tuple[str, str], TerminalLine] = OrderedDict()


def terminal_line(owner: str, session_id: str, *, opened: bool = False) -> TerminalLine:
    """Return the line of the runner owner ``owner``'s terminal ``session_id``; ``opened`` for one just opened."""
    key = (owner, session_id)
    line = _lines.pop(key, None) or TerminalLine(opened=opened)
    _lines[key] = line
    if len(_lines) > _MAX_TERMINALS:
        _lines.popitem(last=False)
    return line


def forget_line(owner: str, session_id: str) -> None:
    """Forget a closed terminal's line."""
    _lines.pop((owner, session_id), None)
