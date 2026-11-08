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
        ("rm -rf ", "<ctrl-c>ls<enter>", [{"args": "ls"}]),
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
    assert (line.state(), line.empty) == (LineState(0, ""), True)
    line.type("rm -rf ")
    assert line.state() == LineState(1, "rm -rf ")
    line.type("tmp\r")
    assert line.state() == LineState(2, None)
    line.prompted(1, 0)
    assert (line.state(), line.empty) == (LineState(2, ""), True)
    line.type("sleep 5\r")
    line.type("echo ahead")
    line.prompted(2, 0)
    assert line.state() == LineState(4, None)
    line.type("\x03")
    assert line.state() == LineState(5, "")
    line.type("ls\x1b[D")
    assert line.state() == LineState(6, None)


def test_ctrl_c_drops_what_a_running_command_left_unread() -> None:
    """The terminal flushes typing a command has not read when Ctrl-C interrupts it, so the next prompt is empty."""
    line = TerminalLine(opened=True)
    line.type("sleep 5\r")
    line.type("typed ahead\x03")
    line.prompted(1, 130)
    assert (line.state(), line.status) == (LineState(2, ""), 130)


def test_a_terminal_the_primary_did_not_see_open_has_an_unknown_line_until_ctrl_c_empties_it() -> None:
    """Its prompt, replayed with the scrollback, says nothing of what was typed after it."""
    line = TerminalLine(opened=False)
    assert line.state() == LineState(0, None)
    line.prompted(4, 0)
    assert line.state() == LineState(0, None)
    line.type("\x03")
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
