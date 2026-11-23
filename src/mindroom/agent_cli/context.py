"""Small explicit bootstrap for the same agent's on-demand context."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_ROSTER_CALL_SYNTAX = (
    "Call one with `mindroom-agent tools call TOOLKIT.FUNCTION '<json arguments>'`; "
    "`mindroom-agent tools describe TOOLKIT.FUNCTION` shows its arguments."
)


def minimal_system_message(
    *,
    agent_name: str,
    display_name: str,
    identity: str | None = None,
    toolkit_names: Sequence[str],
    toolkit_functions: Mapping[str, Sequence[str]] | None = None,
    instructions: Sequence[str],
    context_files: Sequence[str] = (),
    skill_names: Sequence[str] = (),
    memory_root: Path | None = None,
    runtime_context: str = "",
    named_workers: bool = False,
) -> str:
    """Identify the same agent's workspace, memory, and discoverable CLI tools.

    An authored subagent's ``identity`` replaces the agent's own identity line. ``toolkit_functions`` maps toolkit
    names to their known function names, shown inline when within the roster budget. ``named_workers`` says the
    agent's shell commands may run in workers it names, which Bash reaches only through the CLI.
    """
    names = sorted(set(toolkit_names))
    functions_by_toolkit = toolkit_functions or {}
    detailed = "Tools callable through mindroom-agent, by toolkit: " + ", ".join(
        f"{name} ({', '.join(functions)})" if (functions := functions_by_toolkit.get(name)) else name for name in names
    )
    if len(detailed) <= 2000:
        roster = detailed
    else:
        roster = "Toolkits callable through mindroom-agent: " + ", ".join(names)
        if len(roster) > 2000:
            roster = f"{len(names)} toolkits available; use mindroom-agent tools list."
    guidance = []
    if context_files:
        files = "Context files: " + ", ".join(f"`{path}`" for path in context_files)
        if len(files) > 2000:
            files = f"{len(context_files)} context files available; use mindroom-agent context list."
        guidance.append(files)
    if skill_names:
        skills = (
            "Skills: "
            + ", ".join(f"`{name}`" for name in skill_names)
            + "; read one with `mindroom-agent context read skill:<name>`."
        )
        if len(skills) > 2000:
            skills = f"{len(skill_names)} skills available; use mindroom-agent context list."
        guidance.append(skills)
    if memory_root is not None:
        guidance.append(
            f"File memory: `{(memory_root / 'MEMORY.md').as_posix()}`, `{(memory_root / 'memory').as_posix()}/`.",
        )
    if named_workers:
        guidance.append(
            "Named workers: `mindroom-agent workers run NAME -- COMMAND` runs a command in a separate worker of its "
            "own on the same workspace; `mindroom-agent tools describe shell.run_shell_command` explains them.",
        )
    return "\n".join(
        part.strip()
        for part in [
            identity if identity is not None else f"You are {display_name} ({agent_name}) in minimal mode.",
            "Bash starts in your workspace.",
            *guidance,
            "Other tools and full agent context: mindroom-agent --help.",
            roster,
            _ROSTER_CALL_SYNTAX,
            runtime_context,
            *instructions,
        ]
        if part.strip()
    )
