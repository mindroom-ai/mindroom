#!/usr/bin/env python3
"""Script to check if any SECRET VALUES from .env have been committed in git history.

Only searches for values, not keys (since keys naturally appear in code).
"""

import subprocess
from pathlib import Path

import typer
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

console = Console()
app = typer.Typer()


def search_git_history(search_term: str) -> list[str]:
    """Search for a term in git history using git log -S."""
    try:
        # Use git log -S to search for additions/deletions of the term
        result = subprocess.run(
            ["git", "log", "-S", search_term, "--oneline", "--all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.stdout.strip():
            commits = result.stdout.strip().split("\n")
            return commits[:5]  # Return first 5 commits where found
    except (subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass
    return []


def classify_secret(key: str, value: str) -> str:  # noqa: ARG001
    """Classify the sensitivity level of a secret."""
    # High risk patterns
    high_risk = ["API_KEY", "TOKEN", "SECRET", "PRIVATE"]
    medium_risk = ["PASSWORD", "AUTH"]

    key_upper = key.upper()

    if any(pattern in key_upper for pattern in high_risk):
        return "🔴 HIGH"
    if any(pattern in key_upper for pattern in medium_risk):
        return "🟡 MEDIUM"
    return "🟢 LOW"


@app.command()
def scan(  # noqa: C901, PLR0912, PLR0915
    env_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--env-file",
        "-e",
        help="Path to .env file. If not specified, looks for .env in project root.",
    ),
    show_safe: bool = typer.Option(
        True,
        "--show-safe/--hide-safe",
        help="Show secrets that were NOT found in git history",
    ),
    timeout: int = typer.Option(  # noqa: ARG001
        5,
        "--timeout",
        "-t",
        help="Timeout in seconds for each git search operation",
    ),
) -> None:
    """Scan git history for exposed secret values from .env file.

    This tool searches through the entire git history to check if any
    secret VALUES (not keys) from your .env file have been committed.
    """
    console.print(
        Panel.fit(
            "[bold cyan]Git History Secret Scanner[/bold cyan]\n"
            "[dim]Checking for exposed secret values in git history[/dim]",
            border_style="cyan",
        ),
    )

    # Determine .env file path
    if env_file:
        env_path = Path(env_file).resolve()
    else:
        # Find the project root (where .env should be)
        script_path = Path(__file__).resolve()

        # Handle both running from scripts/ or from project root
        project_root = script_path.parent.parent.parent

        env_path = project_root / ".env"

    # Check if env file exists
    if not env_path.exists():
        console.print(f"[red]✗[/red] .env file not found at: {env_path}")
        raise typer.Exit(1)

    # Load .env file
    env_vars = dotenv_values(env_path)

    if not env_vars:
        console.print(f"[red]✗[/red] No environment variables found in {env_path}")
        return

    console.print(f"\n[green]✓[/green] Loaded [bold]{len(env_vars)}[/bold] environment variables from {env_path.name}")
    console.print("[dim]Searching for SECRET VALUES only (not keys)...[/dim]\n")

    found_secrets = []
    not_found_secrets = []

    # Create progress bar
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning git history...", total=len(env_vars))

        # Search for each value only
        for key, value in env_vars.items():
            # Skip empty or very short values
            if not value or len(value) < 5:
                progress.advance(task)
                continue

            # Update progress description
            value_preview = value[:30] + "..." if len(value) > 30 else value
            progress.update(task, description=f"Checking {key}: {value_preview}")

            commits = search_git_history(value)
            risk_level = classify_secret(key, value)

            if commits:
                found_secrets.append((key, value_preview, len(commits), risk_level, commits[:3]))
            else:
                not_found_secrets.append((key, value_preview, risk_level))

            progress.advance(task)

    # Display results
    console.print("\n" + "=" * 60)

    if found_secrets:
        # Create table for found secrets
        console.print("\n[bold red]🚨 EXPOSED SECRETS FOUND![/bold red]\n")

        table = Table(title="Exposed Secret Values in Git History", show_header=True, header_style="bold magenta")
        table.add_column("Risk", style="cyan", width=8)
        table.add_column("Key", style="yellow")
        table.add_column("Value", style="red")
        table.add_column("Commits", style="blue", width=8)
        table.add_column("First Occurrences", style="dim")

        for key, value_preview, count, risk_level, commit_samples in found_secrets:
            commits_str = "\n".join(commit_samples)
            table.add_row(risk_level, key, value_preview, str(count), commits_str)

        console.print(table)

        # Show action required panel
        action_text = Text()
        action_text.append("⚠️  IMMEDIATE ACTION REQUIRED:\n\n", style="bold red")
        action_text.append("1. ", style="bold")
        action_text.append("These secrets should be considered compromised\n")
        action_text.append("2. ", style="bold")
        action_text.append("Rotate/regenerate all exposed API keys immediately\n")
        action_text.append("3. ", style="bold")
        action_text.append("Use 'git filter-repo' or BFG Repo-Cleaner to remove from history\n")
        action_text.append("4. ", style="bold")
        action_text.append("Ensure .env is in .gitignore")

        console.print("\n")
        console.print(Panel(action_text, border_style="red", title="Action Required"))

    else:
        console.print("\n[bold green]✅ EXCELLENT NEWS![/bold green]")
        console.print("[green]No secret VALUES from .env found in git history![/green]\n")

    # Show safe secrets table if requested
    if show_safe and not_found_secrets:
        console.print("\n[bold green]Safe Secrets (Not Found in History)[/bold green]\n")

        safe_table = Table(show_header=True, header_style="bold green")
        safe_table.add_column("Risk", style="cyan", width=8)
        safe_table.add_column("Key", style="green")
        safe_table.add_column("Value", style="dim green")
        safe_table.add_column("Status", style="green")

        for key, value_preview, risk_level in not_found_secrets:
            safe_table.add_row(risk_level, key, value_preview, "✓ Safe")

        console.print(safe_table)

    # Show recommendations
    rec_text = Text()
    rec_text.append("Best Practices:\n\n", style="bold cyan")
    rec_text.append("• ", style="cyan")
    rec_text.append("Keep .env in .gitignore\n")
    rec_text.append("• ", style="cyan")
    rec_text.append("Use git-secrets pre-commit hooks\n")
    rec_text.append("• ", style="cyan")
    rec_text.append("Regularly rotate API keys\n")
    rec_text.append("• ", style="cyan")
    rec_text.append("Use environment-specific secrets management")

    console.print("\n")
    console.print(Panel(rec_text, border_style="cyan", title="Recommendations"))

    # Exit with error if secrets were found
    if found_secrets:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
