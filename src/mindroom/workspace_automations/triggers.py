"""Workspace automation trigger evaluation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mindroom.workspace_automations.models import (
    WorkspaceAutomationTrigger,
    workspace_automation_trigger_has_rule,
)

if TYPE_CHECKING:
    from mindroom.workspace_automations.executor import ShellCheckResult


def trigger_matches(trigger: WorkspaceAutomationTrigger | None, result: ShellCheckResult) -> bool:
    """Return whether a structured shell check result satisfies trigger rules."""
    if trigger is None or not workspace_automation_trigger_has_rule(trigger):
        return False

    return (
        _exit_code_matches(trigger.exit_code, result.exit_code)
        and _positive_regex_matches(trigger.stdout_matches, result.stdout)
        and _positive_regex_matches(trigger.stderr_matches, result.stderr)
        and _negative_regex_matches(trigger.stdout_not_matches, result.stdout)
        and _negative_regex_matches(trigger.stderr_not_matches, result.stderr)
    )


def _exit_code_matches(expected: int | None, actual: int | None) -> bool:
    return expected is None or actual == expected


def _positive_regex_matches(pattern: str | None, output: str) -> bool:
    if pattern is None:
        return True
    return re.search(pattern, output) is not None


def _negative_regex_matches(pattern: str | None, output: str) -> bool:
    if pattern is None:
        return True
    return re.search(pattern, output) is None


__all__ = ["trigger_matches"]
