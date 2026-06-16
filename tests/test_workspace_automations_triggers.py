"""Tests for workspace automation trigger evaluation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.models import WorkspaceAutomationPolicyConfig
from mindroom.workspace_automations.executor import ShellCheckResult
from mindroom.workspace_automations.loader import load_workspace_automations
from mindroom.workspace_automations.models import WorkspaceAutomationTrigger
from mindroom.workspace_automations.triggers import trigger_matches

if TYPE_CHECKING:
    from pathlib import Path


def _result(
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    raw_output: str = "",
) -> ShellCheckResult:
    return ShellCheckResult(
        automation_id="check_repository",
        ok=exit_code == 0,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        raw_output=raw_output,
        timed_out=False,
        error=None,
    )


def _policy() -> WorkspaceAutomationPolicyConfig:
    return WorkspaceAutomationPolicyConfig(
        enabled=True,
        allowed_actions=["agent_message"],
    )


def _write_automations(workspace_root: Path, yaml_text: str) -> None:
    file_path = workspace_root / ".mindroom" / "automations.yaml"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(yaml_text, encoding="utf-8")


def test_none_trigger_does_not_match_visible_actions() -> None:
    """A missing trigger should never match a visible-action check result."""
    assert trigger_matches(None, _result(exit_code=0)) is False


def test_exit_code_trigger_matches_structured_result_exit_code() -> None:
    """Exit-code rules should match the structured shell result exit code."""
    trigger = WorkspaceAutomationTrigger(exit_code=42)

    assert trigger_matches(trigger, _result(exit_code=42)) is True


def test_exit_code_trigger_mismatch_returns_false() -> None:
    """Exit-code rules should fail when the structured exit code differs."""
    trigger = WorkspaceAutomationTrigger(exit_code=42)

    assert trigger_matches(trigger, _result(exit_code=0)) is False


def test_exit_code_trigger_ignores_human_output_text() -> None:
    """Exit-code rules must not parse raw, stdout, or stderr text for exit codes."""
    trigger = WorkspaceAutomationTrigger(exit_code=42)
    result = _result(
        exit_code=0,
        stdout="exit_code=42",
        stderr="process exited with code 42",
        raw_output="Command failed with exit code 42",
    )

    assert trigger_matches(trigger, result) is False


def test_stdout_regex_trigger_uses_re_search() -> None:
    """Stdout regex rules should use search semantics instead of fullmatch."""
    trigger = WorkspaceAutomationTrigger(stdout_matches=r"urgent\s+email")

    assert trigger_matches(trigger, _result(stdout="matched urgent email from ceo")) is True


def test_stderr_regex_trigger_uses_re_search() -> None:
    """Stderr regex rules should use search semantics instead of fullmatch."""
    trigger = WorkspaceAutomationTrigger(stderr_matches=r"Permission denied")

    assert trigger_matches(trigger, _result(stderr="fatal: Permission denied (publickey).")) is True


def test_missing_output_behaves_like_empty_string() -> None:
    """Missing output should not match positives but should satisfy negative regex rules."""
    assert trigger_matches(WorkspaceAutomationTrigger(stdout_matches="urgent"), _result()) is False
    assert trigger_matches(WorkspaceAutomationTrigger(stderr_not_matches="traceback"), _result()) is True


def test_invalid_regex_is_rejected_when_trigger_is_validated() -> None:
    """Invalid regex should fail at model validation time, not trigger evaluation time."""
    with pytest.raises(ValidationError) as exc_info:
        WorkspaceAutomationTrigger(stdout_matches="[")

    assert "stdout_matches" in str(exc_info.value)
    assert "valid regular expression" in str(exc_info.value)


def test_loader_returns_structured_error_for_invalid_trigger_regex(tmp_path: Path) -> None:
    """Invalid regex in YAML should return a structured loader validation error."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  bad_regex:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      stdout_matches: "["
    action:
      type: agent_message
      room: "Lobby"
      message: "Urgent email condition matched."
""",
    )

    result = load_workspace_automations(
        agent_name="ops",
        workspace_root=tmp_path,
        agent_rooms=["Lobby"],
        policy=_policy(),
    )

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "bad_regex"
    assert result.errors[0].field_path == ("automations", "bad_regex", "trigger", "stdout_matches")
    assert "valid regular expression" in result.errors[0].message


def test_combined_triggers_use_and_semantics() -> None:
    """Every provided trigger rule must match."""
    trigger = WorkspaceAutomationTrigger(
        exit_code=42,
        stdout_matches="urgent",
        stderr_not_matches="traceback",
    )

    assert trigger_matches(trigger, _result(exit_code=42, stdout="urgent item", stderr="")) is True
    assert trigger_matches(trigger, _result(exit_code=42, stdout="urgent item", stderr="traceback")) is False
    assert trigger_matches(trigger, _result(exit_code=0, stdout="urgent item", stderr="")) is False


def test_not_matches_triggers_fail_when_pattern_is_found() -> None:
    """Negative regex rules should fail when their pattern is present."""
    trigger = WorkspaceAutomationTrigger(
        stdout_not_matches="noop",
        stderr_not_matches="traceback",
    )

    assert trigger_matches(trigger, _result(stdout="all clear", stderr="")) is True
    assert trigger_matches(trigger, _result(stdout="noop", stderr="")) is False
    assert trigger_matches(trigger, _result(stdout="", stderr="traceback")) is False


def test_loader_accepts_regex_only_visible_trigger(tmp_path: Path) -> None:
    """Visible actions should allow any first-version trigger rule, not only exit_code."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  urgent_email_poll:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      stdout_matches: "urgent"
    action:
      type: agent_message
      room: "Lobby"
      message: "Urgent email condition matched."
""",
    )

    result = load_workspace_automations(
        agent_name="ops",
        workspace_root=tmp_path,
        agent_rooms=["Lobby"],
        policy=_policy(),
    )

    assert result.errors == ()
    assert len(result.automations) == 1
    assert result.automations[0].trigger is not None
    assert result.automations[0].trigger.stdout_matches == "urgent"
