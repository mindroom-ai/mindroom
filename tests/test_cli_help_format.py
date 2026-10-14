"""Tests for the layout of the `mindroom` help pages."""

from __future__ import annotations

from typing import TYPE_CHECKING

import click
import pytest
import typer
from rich.text import Text
from typer import rich_utils
from typer.core import TyperCommand, TyperGroup
from typer.testing import CliRunner

from mindroom.cli import help_format
from mindroom.cli.main import app
from tests.conftest import normalize_console_output

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()
# A summary longer than this wraps in the commands panel of a 100-column terminal.
_SUMMARY_MAX_LENGTH = 72


def _commands(group: click.Group, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    context = click.Context(group)
    for name in group.list_commands(context):
        command = group.get_command(context, name)
        assert command is not None
        yield (*path, name), command
        if isinstance(command, click.Group):
            yield from _commands(command, (*path, name))


def _root() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


def test_every_top_level_command_sits_in_a_named_panel() -> None:
    """The top-level help groups every command, so no catch-all "Commands" panel remains."""
    for path, command in _commands(_root()):
        if len(path) == 1:
            assert isinstance(command, TyperCommand | TyperGroup)
            assert command.rich_help_panel in help_format._HELP_PANELS, path


def test_every_command_panel_is_in_the_panel_order() -> None:
    """A command panel outside `_HELP_PANELS` would have no place in the help order."""
    for path, command in _commands(_root()):
        assert isinstance(command, TyperCommand | TyperGroup)
        panel = command.rich_help_panel
        # Commands without a panel hold Typer's default placeholder instead of a name.
        assert not isinstance(panel, str) or panel in help_format._HELP_PANELS, path


def test_top_level_help_starts_with_getting_started() -> None:
    """The panels follow `_HELP_PANELS`, so the commands a new user needs come first."""
    output = normalize_console_output(runner.invoke(app, ["--help"]).output)

    positions = [output.index(f"{panel} ") for panel in ("Get started", "Configure", "Keep running", "Troubleshoot")]
    assert positions == sorted(positions)
    assert output.index(" run ") < output.index(" config ") < output.index(" service ")
    assert "Commands" not in output


def test_desktop_help_lists_setup_before_advanced_commands() -> None:
    """Commands are ordered by panel even when an advanced one was registered first."""
    output = normalize_console_output(runner.invoke(app, ["desktop", "--help"]).output)

    assert output.index("Set up and run") < output.index("Advanced")
    assert output.index(" setup ") < output.index(" app ")


def test_every_summary_fits_on_one_line() -> None:
    """Each command's first help paragraph is a short summary; details go in later paragraphs."""
    for path, command in _commands(_root()):
        summary = " ".join((command.help or "").split("\n\n")[0].split())
        assert summary, path
        assert len(summary) <= _SUMMARY_MAX_LENGTH, (path, summary)


def test_run_help_groups_its_options_and_shows_examples() -> None:
    """`run --help` splits its options into panels and ends with examples."""
    # A 100-column terminal fits the longest option name in its own panel.
    help_output = runner.invoke(app, ["run", "--help"], env={"TERM": "xterm-256color", "COLUMNS": "100"}).output
    output = normalize_console_output(help_output)

    for panel in ("Paths", "Dashboard", "Deployment"):
        assert f"{panel} " in output
    assert "--bootstrap-config-bundle-revision" in output
    assert "Examples" in output
    assert "mindroom run --provider anthropic" in output


def test_examples_align_their_descriptions() -> None:
    """Every example description starts in the same column, one example per line."""
    epilog = help_format.examples(("mindroom run", "Start"), ("mindroom run --no-api", "Start without the dashboard"))
    # Typer turns each blank-line-separated paragraph of an epilog into one line.
    title, *rows = (Text.from_markup(paragraph).plain for paragraph in epilog.split("\n\n"))

    assert title == "Examples"
    assert rows == ["mindroom run           Start", "mindroom run --no-api  Start without the dashboard"]


@pytest.fixture
def restored_help_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the help theme after the test, since Typer keeps it in module globals."""
    for name in help_format._HELP_THEME:
        monkeypatch.setattr(rich_utils, name, getattr(rich_utils, name))


@pytest.mark.usefixtures("restored_help_theme")
def test_help_theme_sets_styles_typer_still_reads() -> None:
    """A Typer upgrade that renames a style would leave the theme silently unused."""
    styles_before = {name: getattr(rich_utils, name) for name in help_format._HELP_THEME}

    help_format.apply_help_theme()

    for name, style in help_format._HELP_THEME.items():
        assert name in styles_before
        assert getattr(rich_utils, name) == style
