"""Plugin validation commands."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer

from .config import console

plugins_app = typer.Typer(help="Validate external MindRoom plugins.")


@plugins_app.command("check")
def plugin_check(
    path: Path = typer.Argument(  # noqa: B008
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Plugin directory containing mindroom.plugin.json.",
    ),
) -> None:
    """Strictly validate one plugin against this MindRoom version."""
    from mindroom.plugin_check import check_plugin  # noqa: PLC0415

    try:
        result = check_plugin(path)
    except Exception as exc:
        console.print(f"[red]Plugin check failed:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(f"[green]Plugin is compatible:[/green] {result.name}")
    console.print(f"  Tools:  {', '.join(result.tool_names) or 'none'}")
    console.print(f"  Hooks:  {', '.join(result.hook_names) or 'none'}")
    console.print(f"  Skills: {', '.join(result.skill_directories) or 'none'}")
