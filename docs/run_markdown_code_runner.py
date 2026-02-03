#!/usr/bin/env python3
"""Update all markdown files that use markdown-code-runner for auto-generation.

Run from repo root: python docs/run_markdown_code_runner.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console

console = Console()

# Fixed terminal width for reproducible Rich output in CLI --help commands
FIXED_TERMINAL_WIDTH = "90"


def find_markdown_files_with_code_blocks(docs_dir: Path) -> list[Path]:
    """Find all markdown files containing markdown-code-runner markers."""
    files_with_code = []
    for md_file in docs_dir.rglob("*.md"):
        content = md_file.read_text()
        # Match both CODE:START and CODE:BASH:START patterns
        if "<!-- CODE:START -->" in content or "<!-- CODE:BASH:START -->" in content:
            files_with_code.append(md_file)
    return sorted(files_with_code)


def run_markdown_code_runner(files: list[Path], repo_root: Path) -> bool:
    """Run markdown-code-runner on all files. Returns True if all succeeded."""
    if not files:
        console.print("No files with CODE:START markers found.")
        return True

    console.print(f"Found {len(files)} file(s) with auto-generated content:")
    for f in files:
        console.print(f"  - {f.relative_to(repo_root)}")
    console.print()

    # Set fixed terminal width for reproducible Rich/Typer CLI help output
    env = os.environ.copy()
    env["COLUMNS"] = FIXED_TERMINAL_WIDTH  # Rich Console width
    env["TERMINAL_WIDTH"] = FIXED_TERMINAL_WIDTH  # Typer MAX_WIDTH for help panels
    # Prevent Typer from forcing terminal mode in CI (GITHUB_ACTIONS),
    # which treats TERM=dumb as a fixed 80-column terminal.
    env["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

    all_success = True
    for file in files:
        rel_path = file.relative_to(repo_root)
        console.print(f"Updating {rel_path}...", end=" ")
        result = subprocess.run(
            ["markdown-code-runner", str(file)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0:
            console.print("[green]✓[/green]")
        else:
            console.print("[red]✗[/red]")
            console.print(f"  [red]Error:[/red] {result.stderr}")
            all_success = False

    return all_success


def main() -> int:
    """Main entry point."""
    repo_root = Path(__file__).parent.parent

    files = find_markdown_files_with_code_blocks(repo_root)
    success = run_markdown_code_runner(files, repo_root)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
