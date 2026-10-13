"""Tests for the prompts that interactive setup asks in a terminal."""

from __future__ import annotations

import os
import pty
import select
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import typer
from click.testing import CliRunner
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mindroom.cli import interactive

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_CHOICES = (("openai", "OpenAI"), ("anthropic", "Anthropic"), ("ollama", "Ollama"))
_DOWN = "\x1b[B"
_ENTER = "\r"
_CTRL_C = "\x03"


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Answer arrow-key prompts with typed keys, as a person at a terminal would."""
    monkeypatch.setattr(interactive, "_arrow_keys_work", lambda: True)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        yield pipe.send_text


def test_select_moves_from_the_first_choice_with_arrow_keys(keys: Callable[[str], None]) -> None:
    """The pointer starts on the first choice, so two steps down pick the third."""
    keys(_DOWN + _DOWN + _ENTER)

    assert interactive.select("Provider?", _CHOICES) == "ollama"


def test_select_returns_the_first_choice_on_enter(keys: Callable[[str], None]) -> None:
    """Enter alone accepts the first choice, which is the default."""
    keys(_ENTER)

    assert interactive.select("Provider?", _CHOICES) == "openai"


def test_secret_returns_the_typed_text(keys: Callable[[str], None]) -> None:
    """Hidden input still returns what was typed."""
    keys("sk-test" + _ENTER)

    assert interactive.secret("API key") == "sk-test"


def test_secret_returns_empty_text_on_enter(keys: Callable[[str], None]) -> None:
    """Enter alone skips the hidden prompt."""
    keys(_ENTER)

    assert interactive.secret("API key") == ""


@pytest.mark.parametrize(("typed", "expected"), [("n" + _ENTER, False), ("y" + _ENTER, True), (_ENTER, True)])
def test_confirm_reads_yes_no_and_the_default(keys: Callable[[str], None], typed: str, expected: bool) -> None:
    """An answer is confirmed with Enter, as in a line prompt, and Enter alone takes the default."""
    keys(typed)

    assert interactive.confirm("Continue?", default=True) is expected


def test_confirm_answer_waits_for_enter(keys: Callable[[str], None]) -> None:
    """A yes/no answer counts only on Enter, so a corrected key wins and nothing is left for the next question."""
    keys("y" + "n" + _ENTER)

    assert interactive.confirm("Is this your account?", default=True) is False


def test_confirm_answers_only_its_own_question(keys: Callable[[str], None]) -> None:
    """Keys left after an answer, such as a second Enter, must not answer the next question, such as a login service."""
    keys("y" + _ENTER + _ENTER)
    assert interactive.confirm("Is this your account?", default=True) is True

    keys("n" + _ENTER)
    assert interactive.confirm("Run MindRoom in the background?", default=True) is False


@pytest.mark.parametrize(
    "ask",
    [
        lambda: interactive.select("Provider?", _CHOICES),
        lambda: interactive.secret("API key"),
        lambda: interactive.confirm("Continue?", default=True),
    ],
)
def test_ctrl_c_aborts_like_a_line_prompt(keys: Callable[[str], None], ask: Callable[[], object]) -> None:
    """Ctrl+C raises the same abort as typer's prompts, so callers handle both alike."""
    keys(_CTRL_C)

    with pytest.raises(typer.Abort):
        ask()


def test_piped_input_answers_line_prompts() -> None:
    """Arrow-key prompts would silently take the default from a pipe, so piped answers get line prompts."""
    with CliRunner().isolation(input="ollama\nsk-test\nn\n"):
        assert interactive.select("Provider?", _CHOICES) == "ollama"
        assert interactive.secret("API key") == "sk-test"
        assert interactive.confirm("Continue?", default=True) is False


def test_piped_select_asks_again_after_an_unknown_choice() -> None:
    """A line answer must name one of the choices."""
    with CliRunner().isolation(input="gemini\nanthropic\n") as (_, _, output):
        assert interactive.select("Provider?", _CHOICES) == "anthropic"
        assert b"is not one of" in output.getvalue()


def test_terminal_is_interactive_requires_stdin_and_stdout_ttys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Services and piped runs never get arrow-key prompts, even when only one stream is a terminal."""
    for stdin_tty, stdout_tty, expected in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda value=stdin_tty: value))
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda value=stdout_tty: value))
        assert interactive.terminal_is_interactive() is expected


@pytest.mark.parametrize(("term", "expected"), [("xterm-256color", True), ("dumb", False), ("unknown", False)])
def test_dumb_terminals_get_line_prompts(monkeypatch: pytest.MonkeyPatch, term: str, expected: bool) -> None:
    """Terminals such as Emacs shell buffers cannot draw arrow-key prompts, so they get line prompts."""
    monkeypatch.setattr(interactive, "terminal_is_interactive", lambda: True)
    monkeypatch.setenv("TERM", term)

    assert interactive._arrow_keys_work() is expected


def test_keys_typed_before_a_prompt_are_not_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An Enter pressed while pairing waits must not accept "Is this your account?" once it appears."""
    primary, secondary = pty.openpty()
    with os.fdopen(secondary, "r") as terminal_input:
        monkeypatch.setattr(sys, "stdin", terminal_input)
        os.write(primary, b"\r")
        assert select.select([terminal_input], [], [], 1)[0]

        interactive._discard_typeahead()

        assert not select.select([terminal_input], [], [], 0)[0]
    os.close(primary)


def test_every_prompt_discards_typeahead_before_asking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each prompt drops earlier keys first, so none of them answers with a stray Enter."""
    discarded: list[str] = []
    monkeypatch.setattr(interactive, "_discard_typeahead", lambda: discarded.append("discard"))
    with CliRunner().isolation(input="ollama\nsk-test\nn\n"):
        interactive.select("Provider?", _CHOICES)
        interactive.secret("API key")
        interactive.confirm("Continue?", default=True)

    assert discarded == ["discard", "discard", "discard"]


def test_piped_answers_survive_the_typeahead_discard() -> None:
    """Only terminal input is discarded; answers piped in ahead of time are still read."""
    with CliRunner().isolation(input="n\n"):
        interactive._discard_typeahead()
        assert interactive.confirm("Continue?", default=True) is False
