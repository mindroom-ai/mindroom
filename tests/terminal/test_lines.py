"""A terminal's line as the primary records it, and the shell command typing onto it amounts to."""

from __future__ import annotations

import pytest

from mindroom.terminal.lines import LineState, PromptMarks, TerminalLine, entered_command, keys_to_text


def _mark(number: int, status: int = 0) -> bytes:
    return f"\x1b]133;D;{status};mindroom={number}\x07".encode()


@pytest.mark.parametrize(
    ("typed", "keys", "command"),
    [
        ("", "ls<enter>", [{"args": "ls"}]),
        ("rm -rf ", "tmp<enter>", [{"args": "rm -rf tmp"}]),
        ("rm -rf tmp", "<enter>", [{"args": "rm -rf tmp"}]),
        ("", "rm -rf ", []),
        ("anything", "<ctrl-c>", []),
        (None, "<ctrl-c>", []),
        # The terminal may drop part of the keys typed right after a Ctrl-C.
        ("rm -rf ", "<ctrl-c>ls<enter>", None),
        (None, "<ctrl-c>ls<enter>", None),
        ("", "make<enter>echo done<enter>more", [{"args": "make\necho done"}]),
        (None, "ls<enter>", None),
        ("", "<up><enter>", None),
        ("", "rm<tab><enter>", None),
        ("", "ls<enter><ctrl-c>", None),
        ("", "\x1b[200~rm -rf tmp\x1b[201~<enter>", None),
    ],
)
def test_typing_amounts_to_the_command_its_enters_run(typed: str | None, keys: str, command: object) -> None:
    """What the Enters run is judged whole, the earlier line included; a line or key it can't read tells nothing."""
    assert entered_command(typed, keys_to_text(keys)) == command


def test_named_keys_are_keys_and_other_text_is_typed_as_written() -> None:
    """A key name it does not know, or a key without its brackets, is text."""
    assert keys_to_text("y<Enter><ctrl-d><ctrl-->< up><left>") == "y\r\x04<ctrl-->< up>\x1b[D"


def test_the_line_follows_typing_from_anyone_until_its_enter_and_the_next_prompt() -> None:
    """After Enter the line is unknown until the shell prompts again; typing meant for that prompt leaves it unknown."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    assert (line.state(), line.empty) == (LineState(0, ""), True)
    line.type("rm -rf ")
    assert line.state() == LineState(1, "rm -rf ")
    line.type("tmp\r")
    assert line.state() == LineState(2, None)
    line.prompted(2)
    assert (line.state(), line.empty) == (LineState(2, ""), True)
    line.type("sleep 5\r")
    line.type("echo ahead")
    line.prompted(3)
    assert line.state() == LineState(4, None)
    line.type("\x03")
    line.prompted(4)
    assert line.state() == LineState(5, "")
    line.type("ls\x1b[D")
    assert line.state() == LineState(6, None)


def test_a_prompt_counts_once_and_a_lost_send_leaves_the_next_prompt_unknown() -> None:
    """A mark replayed with another viewer's scrollback ends no command, and a lost send may have typed ahead."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("sleep 5\r")
    line.prompted(1)
    assert line.state() == LineState(1, None)
    line.prompted(2)
    assert line.state() == LineState(1, "")
    line.lost()
    line.prompted(3)
    assert line.state() == LineState(2, None)


def test_keys_sent_with_a_ctrl_c_at_the_prompt_leave_the_line_unknown_until_a_ctrl_c_alone() -> None:
    """The terminal may drop part of them, and the shell then prints no prompt for that Ctrl-C."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("abc\x03Xrm -rf tmp")
    assert line.state() == LineState(1, None)
    line.type("\x03")
    line.prompted(2)
    assert line.state() == LineState(2, "")
    line.type("abc\x03echo hi\r")
    line.prompted(3)
    assert line.state() == LineState(3, None)


def test_ctrl_c_drops_what_a_running_command_left_unread() -> None:
    """The terminal flushes typing a command has not read when Ctrl-C interrupts it, so the next prompt is empty."""
    line = TerminalLine(opened=True)
    line.prompted(1)
    line.type("sleep 5\r")
    line.type("typed ahead\x03")
    line.prompted(2)
    assert line.state() == LineState(2, "")


def test_a_prompt_the_shell_owed_ends_no_command_and_typing_before_it_is_unknown() -> None:
    """The shell's first prompt, and the one after a Ctrl-C, come before it reads a command typed now.

    Ctrl-C empties the line only at its prompt: typing that reaches the shell before may be dropped, or keep the shell
    from printing that prompt.
    """
    line = TerminalLine(opened=True)
    assert line.awaited == 1
    line.type("\x03")
    assert (line.state(), line.empty, line.awaited) == (LineState(1, None), False, 2)
    line.prompted(1)
    assert line.state() == LineState(1, None)
    line.prompted(2)
    assert (line.state(), line.empty, line.awaited) == (LineState(1, ""), True, 2)
    line.type("\x03")
    line.type("ls\r")
    line.prompted(3)
    assert line.state() == LineState(3, None)


def test_a_terminal_the_primary_did_not_see_open_has_an_unknown_line_until_ctrl_c_empties_it() -> None:
    """Its prompt, replayed with the scrollback, says nothing of what was typed after it."""
    line = TerminalLine(opened=False)
    assert line.state() == LineState(0, None)
    line.prompted(4)
    assert line.state() == LineState(0, None)
    line.type("\x03")
    line.prompted(5)
    assert line.state() == LineState(1, "")


def test_a_line_judged_before_anyone_typed_on_it_is_unchanged_only_until_they_do() -> None:
    """A description that claims other text than the line holds never matches, and a lost send leaves it unknown."""
    line = TerminalLine(opened=True)
    line.type("rm -rf ")
    judged = line.state()
    assert line.unchanged(judged)
    assert not line.unchanged(LineState(judged.revision, "ls "))
    line.type("tmp")
    assert not line.unchanged(judged)
    line.lost()
    assert line.state() == LineState(3, None)


def test_prompt_marks_count_once_even_split_across_frames() -> None:
    """A mark the shell printed across two frames is found once both arrive."""
    marks = PromptMarks()
    whole = b"out" + _mark(7, 1) + b"bash$ "
    assert marks.feed(whole[:9]) == []
    assert marks.feed(whole[9:]) == [(7, 1)]
    assert marks.feed(b"\x1b[?2004h" + _mark(8)) == [(8, 0)]
