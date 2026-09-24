"""Small explicit bootstrap for the same agent's on-demand context."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def minimal_system_message(
    *,
    agent_name: str,
    display_name: str,
    toolkit_names: Sequence[str],
    instructions: Sequence[str],
    context_files: Sequence[str] = (),
    memory_root: Path | None = None,
    runtime_context: str = "",
) -> str:
    """Identify the same agent's workspace, memory, and discoverable CLI tools."""
    names = sorted(set(toolkit_names))
    roster = "Toolkits callable through mindroom-agent: " + ", ".join(names)
    if len(roster) > 2000:
        roster = f"{len(names)} toolkits available; use mindroom-agent tools list."
    guidance = []
    if context_files:
        files = "Context files: " + ", ".join(f"`{path}`" for path in context_files)
        if len(files) > 2000:
            files = f"{len(context_files)} context files available; use mindroom-agent context list."
        guidance.append(files)
    if memory_root is not None:
        guidance.append(
            f"File memory: `{(memory_root / 'MEMORY.md').as_posix()}`, `{(memory_root / 'memory').as_posix()}/`.",
        )
    return "\n".join(
        part
        for part in [
            f"You are {display_name} ({agent_name}) in minimal mode.",
            "Bash starts in your workspace.",
            *guidance,
            "Other tools and full agent context: mindroom-agent --help.",
            roster,
            runtime_context,
            *instructions,
        ]
        if part
    )
