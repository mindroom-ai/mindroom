"""A terminal's line as the primary records it, and the shell's prompts it counts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.terminal import lines
from mindroom.terminal.lines import PromptMarks, TerminalLine, forget_line, terminal_line

if TYPE_CHECKING:
    import pytest


def _mark(number: int, status: int = 0) -> bytes:
    return f"\x1b]133;D;{status};mindroom={number}\x07".encode()


def test_the_line_is_empty_only_at_a_prompt_with_nothing_typed_since() -> None:
    """After Enter the line waits for the shell's next prompt; typing meant for that prompt is then on its line."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    assert line.empty
    line.type("rm -rf ")
    assert not line.empty
    line.type("tmp\r")
    assert not line.empty
    line.prompted(2)
    assert line.empty
    line.type("sleep 5\r")
    line.type("echo ahead")
    line.prompted(3)
    assert not line.empty
    line.type("\x03")
    line.prompted(4)
    assert line.empty


def test_a_prompt_counts_once_and_a_lost_send_keeps_the_shell_busy_until_a_ctrl_c() -> None:
    """A mark replayed with another viewer's scrollback ends no command, and a lost send may have queued lines."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("sleep 5\r")
    line.prompted(1)
    assert not line.empty
    line.prompted(2)
    assert line.empty
    line.lost()
    line.prompted(3)
    line.type("ls\r")
    line.prompted(4)
    assert not line.empty
    line.type("\x03")
    line.prompted(5)
    assert line.empty


def test_keys_sent_with_a_ctrl_c_at_the_prompt_leave_the_line_holding_typing_until_a_ctrl_c_alone() -> None:
    """The terminal may drop part of them, and the shell then prints no prompt for that Ctrl-C."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("abc\x03Xrm -rf tmp")
    assert not line.empty
    line.type("\x03")
    line.prompted(2)
    assert line.empty
    line.type("abc\x03echo hi\r")
    line.prompted(3)
    assert not line.empty


def test_ctrl_c_drops_what_a_running_command_left_unread() -> None:
    """The terminal flushes typing a command has not read when Ctrl-C interrupts it, so the next prompt is empty."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("sleep 5\r")
    line.type("typed ahead\x03")
    line.prompted(2)
    assert line.empty


def test_a_prompt_the_shell_owed_ends_no_command_and_typing_before_it_stays_on_the_line() -> None:
    """The shell's first prompt, and the one after a Ctrl-C, come before it reads a command typed now.

    Ctrl-C empties the line only at its prompt: typing that reaches the shell before may be dropped, or keep the shell
    from printing that prompt.
    """
    line = TerminalLine(opened=True)
    assert line.awaited == 1
    line.type("\x03")
    assert (line.empty, line.awaited) == (False, 2)
    line.prompted(1)
    assert not line.empty
    line.prompted(2)
    assert (line.empty, line.awaited) == (True, 2)
    line.type("\x03")
    line.type("ls\r")
    line.prompted(3)
    assert not line.empty


def test_a_terminal_the_primary_did_not_see_open_holds_typing_until_ctrl_c_empties_it() -> None:
    """Its prompt, replayed with the scrollback, says nothing of what was typed after it."""
    line = TerminalLine(opened=False)
    assert not line.empty
    line.prompted(4)
    assert not line.empty
    line.type("\x03")
    line.prompted(5)
    assert line.empty


def test_an_enter_typed_while_a_command_runs_keeps_the_shell_busy_until_a_ctrl_c() -> None:
    """The shell runs typed-ahead lines one after another, so no prompt after them says it waits at an empty one."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("npm run build\r")
    line.type("npm test\r")
    line.prompted(2)
    assert not line.empty
    line.type("npm run lint\r")
    line.prompted(3)
    assert not line.empty
    line.type("\x03")
    line.prompted(4)
    assert line.empty
    # A terminal the primary did not see open may be running such lines too.
    unseen = TerminalLine(opened=False)
    unseen.prompted(7)
    unseen.type("npm test\r")
    unseen.prompted(8)
    assert not unseen.empty


def test_prompt_marks_count_once_even_split_across_frames() -> None:
    """A mark the shell printed across two frames is found once both arrive."""
    marks = PromptMarks()
    whole = b"out" + _mark(7, 1) + b"bash$ "
    assert marks.feed(whole[:9]) == []
    assert marks.feed(whole[9:]) == [7]
    assert marks.feed(b"\x1b[?2004h" + _mark(8)) == [8]


def test_each_terminal_keeps_its_line_until_closed_or_least_recently_used_past_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every viewer and call of a terminal shares its line; one the primary did not see open starts holding typing."""
    monkeypatch.setattr(lines, "_MAX_TERMINALS", 2)
    first = terminal_line("lru", "a", opened=True)
    assert terminal_line("lru", "a") is first
    assert first.empty
    second = terminal_line("lru", "b")
    assert not second.empty
    assert terminal_line("lru", "a") is first
    terminal_line("lru", "c")
    assert terminal_line("lru", "a") is first
    assert terminal_line("lru", "b") is not second
    forget_line("lru", "a")
    assert terminal_line("lru", "a") is not first
