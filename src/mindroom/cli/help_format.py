"""Shared look of the `mindroom` help pages: panel order, colors, and examples."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from typer.core import TyperCommand, TyperGroup

if TYPE_CHECKING:
    import click

# Command panels in the order help pages show them; commands without a panel come first.
_HELP_PANELS = (
    # Top level
    "Get started",
    "Configure",
    "Keep running",
    "Desktop",
    "Troubleshoot",
    "Automation",
    "Development",
    # `config`
    "Create and edit",
    "Inspect",
    "Deployment",
    # `desktop`
    "Set up and run",
    "Advanced",
)

# Typer's help styles, in MindRoom's green from the banner and the setup headers.
_HELP_THEME = {
    "STYLE_COMMANDS_PANEL_BORDER": "green",
    "STYLE_OPTIONS_PANEL_BORDER": "green",
    "STYLE_COMMANDS_TABLE_FIRST_COLUMN": "bold green",
    "STYLE_USAGE": "bold green",
}


class PanelOrderedGroup(TyperGroup):
    """A command group listed panel by panel in `_HELP_PANELS` order, keeping the command order within a panel.

    Typer shows panels in the order their first command appears, and it lists commands before command groups.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Return the subcommand names sorted by their panel's place in `_HELP_PANELS`."""
        return sorted(super().list_commands(ctx), key=lambda name: _panel_rank(self.commands[name]))


def _panel_rank(command: click.Command) -> int:
    panel = command.rich_help_panel if isinstance(command, TyperCommand | TyperGroup) else None
    # A command without a panel holds Typer's default placeholder rather than a panel name.
    return _HELP_PANELS.index(panel) if isinstance(panel, str) else -1


def apply_help_theme() -> None:
    """Color Typer's help pages with `_HELP_THEME`."""
    from typer import rich_utils  # noqa: PLC0415  # lazy: Typer loads its help renderer only to show help

    for name, style in _HELP_THEME.items():
        setattr(rich_utils, name, style)


def examples(*rows: tuple[str, str]) -> str:
    """Return a help epilog listing `(command, what it does)` examples with aligned descriptions."""
    width = max(len(command) for command, _ in rows) + 2
    lines = [f"[green]{escape(command)}[/green]{' ' * (width - len(command))}{escape(what)}" for command, what in rows]
    # Typer prints each blank-line-separated paragraph of an epilog as one line.
    return "\n\n".join(["[bold]Examples[/bold]", *lines])
