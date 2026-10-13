"""Prompts and step headers for the setup that a person answers in a terminal."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import click
import typer
from rich.console import Group, NewLine
from rich.rule import Rule

if TYPE_CHECKING:
    from collections.abc import Sequence

    from questionary import Question


def section(title: str) -> Group:
    """Return the header, after one blank line, that starts one setup step."""
    return Group(NewLine(), Rule(f"[bold]{title}[/bold]", align="left", style="green"))


def select[Value: str](message: str, choices: Sequence[tuple[Value, str]]) -> Value:
    """Return the value of the `(value, title)` choice picked with the arrow keys, or typed by value when piped.

    The first choice is the default; questionary would mark any other default as picked next to the pointer.
    """
    if _arrow_keys_work():
        import questionary  # noqa: PLC0415  # lazy: prompt_toolkit loads only for prompts

        picked = _ask(
            questionary.select(
                message,
                choices=[questionary.Choice(title, value=value) for value, title in choices],
                # The pointed choice gets the green of the setup headers; questionary leaves it unstyled.
                style=questionary.Style([("pointer", "fg:ansigreen bold"), ("highlighted", "fg:ansigreen bold")]),
            ),
        )
    else:
        picked = typer.prompt(message, default=choices[0][0], type=click.Choice([value for value, _ in choices]))
    return next(value for value, _ in choices if value == picked)


def secret(message: str) -> str:
    """Return hidden text typed after `message`, or an empty string when Enter is pressed alone."""
    if _arrow_keys_work():
        import questionary  # noqa: PLC0415  # lazy: prompt_toolkit loads only for prompts

        return str(_ask(questionary.password(message)))
    return typer.prompt(message, default="", show_default=False, hide_input=True)


def confirm(message: str, *, default: bool) -> bool:
    """Return whether the yes/no question was answered yes."""
    if _arrow_keys_work():
        import questionary  # noqa: PLC0415  # lazy: prompt_toolkit loads only for prompts

        # Like a line prompt, an answer waits for Enter; otherwise the Enter typed after `y` answers the next question.
        return _ask(questionary.confirm(message, default=default, auto_enter=False)) is True
    return typer.confirm(message, default=default)


def terminal_is_interactive() -> bool:
    """Whether a person can answer prompts and see their output; services, Docker, and the macOS app cannot."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _arrow_keys_work() -> bool:
    """Whether arrow-key prompts work here; they take defaults from piped input and cannot draw in dumb terminals."""
    return terminal_is_interactive() and os.environ.get("TERM", "").lower() not in {"dumb", "unknown"}


def _ask(question: Question) -> object:
    """Ask `question`, raising `typer.Abort` on Ctrl+C or end of input the way typer's prompts do."""
    try:
        return question.unsafe_ask()
    except (KeyboardInterrupt, EOFError):
        raise typer.Abort from None
