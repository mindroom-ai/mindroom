"""Tests for workspace automation YAML loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.models import WorkspaceAutomationPolicyConfig
from mindroom.workspace_automations import loader
from mindroom.workspace_automations.loader import load_workspace_automations

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest

    from mindroom.workspace_automations.models import WorkspaceAutomationLoadResult


def _policy(
    *,
    enabled: bool = True,
    max_timeout_seconds: int = 30,
    min_interval_seconds: int = 60,
    allowed_actions: list[str] | None = None,
) -> WorkspaceAutomationPolicyConfig:
    return WorkspaceAutomationPolicyConfig(
        enabled=enabled,
        max_timeout_seconds=max_timeout_seconds,
        min_interval_seconds=min_interval_seconds,
        allowed_actions=allowed_actions if allowed_actions is not None else ["agent_message", "matrix_message", "hook"],
    )


def _write_automations(workspace_root: Path, yaml_text: str) -> Path:
    file_path = workspace_root / ".mindroom" / "automations.yaml"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(yaml_text, encoding="utf-8")
    return file_path


def _load(
    workspace_root: Path,
    *,
    policy: WorkspaceAutomationPolicyConfig | None = None,
    rooms: list[str] | None = None,
) -> WorkspaceAutomationLoadResult:
    return load_workspace_automations(
        agent_name="ops",
        workspace_root=workspace_root,
        agent_rooms=rooms if rooms is not None else ["Lobby"],
        policy=policy or _policy(),
    )


def test_missing_automation_file_returns_empty_result(tmp_path: Path) -> None:
    """Missing workspace automation files should be treated as no automations."""
    result = _load(tmp_path)

    assert result.automations == ()
    assert result.errors == ()


def test_symlinked_automation_file_returns_structured_error(tmp_path: Path) -> None:
    """Workspace automation files should not follow symlinks out of the workspace."""
    outside_file = tmp_path / "outside.yaml"
    outside_file.write_text("version: 1\nautomations: {}\n", encoding="utf-8")
    automation_file = tmp_path / ".mindroom" / "automations.yaml"
    automation_file.parent.mkdir(parents=True)
    automation_file.symlink_to(outside_file)

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert "workspace root" in result.errors[0].message


def test_oversized_automation_file_returns_structured_error(tmp_path: Path) -> None:
    """Workspace automation files should be capped before YAML parsing."""
    automation_file = tmp_path / ".mindroom" / "automations.yaml"
    automation_file.parent.mkdir(parents=True)
    automation_file.write_text("x" * (loader._MAX_AUTOMATIONS_FILE_BYTES + 1), encoding="utf-8")

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert "must not exceed" in result.errors[0].message


def test_directory_automation_file_returns_structured_error(tmp_path: Path) -> None:
    """Workspace automation path must be a regular file, not a directory or device."""
    automation_file = tmp_path / ".mindroom" / "automations.yaml"
    automation_file.mkdir(parents=True)

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert "regular file" in result.errors[0].message


def test_disabled_policy_returns_empty_result_without_reading_file(tmp_path: Path) -> None:
    """Policy-disabled agents should ignore workspace-authored automations."""
    _write_automations(tmp_path, "this: [is: not: valid")

    result = _load(tmp_path, policy=_policy(enabled=False))

    assert result.automations == ()
    assert result.errors == ()


def test_invalid_yaml_returns_structured_parse_error(tmp_path: Path) -> None:
    """Invalid enabled automation YAML should return a structured parse error."""
    _write_automations(tmp_path, "version: [1\n")

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert result.errors[0].field_path == ()
    assert "Could not parse automation YAML" in result.errors[0].message


def test_valid_yaml_loads_normalized_automation(tmp_path: Path) -> None:
    """A valid automation file should return a normalized runtime record."""
    file_path = _write_automations(
        tmp_path,
        """
version: 1
automations:
  urgent_email_poll:
    enabled: true
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "./scripts/check_urgent_email.sh"
      timeout_seconds: 20
      tail: 100
    trigger:
      exit_code: 42
    action:
      type: agent_message
      room: "Lobby"
      thread_id: null
      message: "Urgent email condition matched. Investigate and summarize."
""",
    )

    result = _load(tmp_path, rooms=["Lobby", "Ops"])

    assert result.errors == ()
    assert len(result.automations) == 1
    automation = result.automations[0]
    assert automation.agent_name == "ops"
    assert automation.automation_id == "urgent_email_poll"
    assert automation.workspace_root == tmp_path
    assert automation.file_path == file_path
    assert automation.schedule == "*/1 * * * *"
    assert automation.check.type == "shell"
    assert automation.check.command == "./scripts/check_urgent_email.sh"
    assert automation.check.timeout_seconds == 20
    assert automation.check.tail == 100
    assert automation.trigger is not None
    assert automation.trigger.exit_code == 42
    assert automation.action.type == "agent_message"
    assert automation.action.room == "Lobby"
    assert automation.action.thread_id is None
    assert automation.action.message == "Urgent email condition matched. Investigate and summarize."


def test_invalid_version_returns_structured_error(tmp_path: Path) -> None:
    """Only version 1 automation files should be accepted."""
    _write_automations(
        tmp_path,
        """
version: 2
automations: {}
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert result.errors[0].field_path == ("version",)
    assert "version" in result.errors[0].message


def test_bool_version_returns_structured_error(tmp_path: Path) -> None:
    """YAML booleans should not satisfy the required integer version."""
    _write_automations(
        tmp_path,
        """
version: true
automations: {}
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert result.errors[0].field_path == ("version",)
    assert "integer 1" in result.errors[0].message


def test_float_version_returns_structured_error(tmp_path: Path) -> None:
    """YAML floats should not satisfy the required integer version."""
    _write_automations(
        tmp_path,
        """
version: 1.0
automations: {}
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id is None
    assert result.errors[0].field_path == ("version",)
    assert "integer 1" in result.errors[0].message


def test_invalid_automation_id_returns_structured_error(tmp_path: Path) -> None:
    """Automation IDs should be single path-safe identifiers."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  ../unsafe:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "../unsafe"
    assert result.errors[0].field_path == ("automations", "../unsafe")
    assert "path-safe" in result.errors[0].message


def test_invalid_schedule_returns_structured_error(tmp_path: Path) -> None:
    """Schedules should be valid five-field cron expressions."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  bad_schedule:
    schedule: "* * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "bad_schedule"
    assert result.errors[0].field_path == ("automations", "bad_schedule", "schedule")
    assert "five-field cron" in result.errors[0].message


def test_impossible_schedule_returns_structured_error(tmp_path: Path) -> None:
    """Schedules that cannot produce runs should return a structured loader error."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  impossible_schedule:
    schedule: "0 0 31 2 *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "impossible_schedule"
    assert result.errors[0].field_path == ("automations", "impossible_schedule", "schedule")
    assert "cannot produce" in result.errors[0].message


def test_leap_year_schedule_gap_below_policy_limit_returns_structured_error(tmp_path: Path) -> None:
    """Cron interval validation should see Feb 28/29 leap-year adjacent runs."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  leap_year_gap:
    schedule: "0 0 28,29 2 *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path, policy=_policy(min_interval_seconds=172800))

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "leap_year_gap"
    assert result.errors[0].field_path == ("automations", "leap_year_gap", "schedule")
    assert "min_interval_seconds 172800" in result.errors[0].message


def test_timeout_above_policy_limit_returns_structured_error(tmp_path: Path) -> None:
    """Check timeout should not exceed the effective policy cap."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  slow_check:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "sleep 60"
      timeout_seconds: 31
    action:
      type: none
""",
    )

    result = _load(tmp_path, policy=_policy(max_timeout_seconds=30))

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "slow_check"
    assert result.errors[0].field_path == ("automations", "slow_check", "check", "timeout_seconds")
    assert "max_timeout_seconds" in result.errors[0].message


def test_disallowed_visible_action_returns_structured_error(tmp_path: Path) -> None:
    """Visible actions should be explicitly allowed by policy."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  announce:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: matrix_message
      room: "Lobby"
      message: "Done"
""",
    )

    result = _load(tmp_path, policy=_policy(allowed_actions=["agent_message"]))

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "announce"
    assert result.errors[0].field_path == ("automations", "announce", "action", "type")
    assert "not allowed" in result.errors[0].message


def test_visible_action_missing_trigger_returns_structured_error(tmp_path: Path) -> None:
    """Visible actions should require an explicit trigger."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  unguarded:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: agent_message
      room: "Lobby"
      message: "Done"
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "unguarded"
    assert result.errors[0].field_path == ("automations", "unguarded", "trigger")
    assert "trigger" in result.errors[0].message


def test_visible_actions_without_message_return_structured_errors(tmp_path: Path) -> None:
    """Visible Matrix actions should require an explicit message at load time."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  missing_matrix_message:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: matrix_message
      room: "Lobby"
  missing_agent_message:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: agent_message
      room: "Lobby"
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert len(result.errors) == 2
    assert {error.field_path for error in result.errors} == {
        ("automations", "missing_matrix_message", "action", "message"),
        ("automations", "missing_agent_message", "action", "message"),
    }
    assert all("action.message" in error.message for error in result.errors)


def test_action_none_does_not_require_trigger_or_policy_action(tmp_path: Path) -> None:
    """The none action should be allowed without a trigger and without policy opt-in."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  quiet_probe:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    policy = _policy(allowed_actions=[])
    assert policy.allowed_actions == []

    result = _load(tmp_path, policy=policy)

    assert result.errors == ()
    assert len(result.automations) == 1
    assert result.automations[0].trigger is None
    assert result.automations[0].action.type == "none"


def test_automation_enabled_defaults_true_and_disabled_entries_are_skipped(tmp_path: Path) -> None:
    """Omitted enabled should load, while explicitly disabled entries should be skipped."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  default_enabled:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
  disabled_probe:
    enabled: false
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "false"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert result.errors == ()
    assert [automation.automation_id for automation in result.automations] == ["default_enabled"]


def test_disabled_automation_with_invalid_fields_is_skipped_without_errors(tmp_path: Path) -> None:
    """Disabled entries should not validate fields that would matter only when enabled."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  disabled_broken_probe:
    enabled: false
    schedule: "not cron"
""",
    )

    result = _load(tmp_path)

    assert result.automations == ()
    assert result.errors == ()


def test_output_tail_defaults_to_one_hundred_lines(tmp_path: Path) -> None:
    """Shell check output tail should default to a bounded value."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  default_tail:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert result.errors == ()
    assert result.automations[0].check.tail == 100


def test_single_agent_room_is_used_when_visible_action_omits_room(tmp_path: Path) -> None:
    """Visible Matrix actions may inherit the sole configured agent room."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  fallback_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: matrix_message
      message: "Done"
""",
    )

    result = _load(tmp_path, rooms=["Ops"])

    assert result.errors == ()
    assert result.automations[0].action.room == "Ops"


def test_single_agent_room_is_used_when_hook_action_omits_room(tmp_path: Path) -> None:
    """Hook actions may inherit the sole configured agent room for room-scoped hooks."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  fallback_hook_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: hook
""",
    )

    result = _load(tmp_path, rooms=["Ops"])

    assert result.errors == ()
    assert result.automations[0].action.room == "Ops"


def test_hook_action_may_omit_room_when_agent_has_multiple_rooms(tmp_path: Path) -> None:
    """Roomless hook actions should stay roomless when no single fallback exists."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  roomless_hook:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: hook
""",
    )

    result = _load(tmp_path, rooms=["Ops", "Alerts"])

    assert result.errors == ()
    assert result.automations[0].action.room is None


def test_hook_action_may_omit_room_when_agent_has_no_rooms(tmp_path: Path) -> None:
    """Roomless hooks should still work for unscoped hook observers."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  unscoped_hook:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: hook
""",
    )

    result = _load(tmp_path, rooms=[])

    assert result.errors == ()
    assert result.automations[0].action.room is None


def test_hook_action_room_must_belong_to_owning_agent(tmp_path: Path) -> None:
    """Hook action room scopes should not spoof unrelated configured rooms."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  foreign_hook_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: hook
      room: "Security"
""",
    )

    result = _load(tmp_path, rooms=["Ops"])

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "foreign_hook_room"
    assert result.errors[0].field_path == ("automations", "foreign_hook_room", "action", "room")
    assert "owning agent" in result.errors[0].message


def test_single_agent_room_fallback_uses_shared_target_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Loader room fallback should use the same pure helper as action execution."""
    calls: list[tuple[str | None, tuple[str, ...]]] = []

    def resolve_action_room(action_room: str | None, agent_configured_rooms: Sequence[str]) -> str | None:
        calls.append((action_room, tuple(agent_configured_rooms)))
        return "Ops"

    monkeypatch.setattr(loader, "resolve_action_room", resolve_action_room)
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  fallback_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: matrix_message
      message: "Done"
""",
    )

    result = _load(tmp_path, rooms=["SharedRoom", "Ops"])

    assert calls == [(None, ("SharedRoom", "Ops"))]
    assert result.errors == ()
    assert result.automations[0].action.room == "Ops"


def test_visible_action_without_room_errors_when_agent_has_multiple_rooms(tmp_path: Path) -> None:
    """Visible Matrix actions should not guess between multiple configured rooms."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  ambiguous_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: agent_message
      message: "Done"
""",
    )

    result = _load(tmp_path, rooms=["Lobby", "Ops"])

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].field_path == ("automations", "ambiguous_room", "action", "room")
    assert "room" in result.errors[0].message


def test_irregular_schedule_below_min_interval_returns_structured_error(tmp_path: Path) -> None:
    """The minimum gap (not just the first gap) must satisfy the interval policy.

    ``0 0 1,2 * *`` has a ~30-day first gap but actually fires one day apart
    (the 1st then the 2nd), so a policy floor above one day must reject it.
    """
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  irregular:
    schedule: "0 0 1,2 * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path, policy=_policy(min_interval_seconds=2 * 24 * 60 * 60))

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].field_path == ("automations", "irregular", "schedule")
    assert "min_interval_seconds" in result.errors[0].message


def test_visible_action_room_outside_agent_rooms_returns_structured_error(tmp_path: Path) -> None:
    """A workspace must not target a room outside the owning agent's configured rooms."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  escalate_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: matrix_message
      room: "Secret"
      message: "Done"
""",
    )

    result = _load(tmp_path, rooms=["Lobby", "Ops"])

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].field_path == ("automations", "escalate_room", "action", "room")
    assert "configured rooms" in result.errors[0].message


def test_visible_action_without_room_errors_when_agent_has_no_rooms(tmp_path: Path) -> None:
    """Visible Matrix actions should not invent a room for agents with no rooms."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  missing_room:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    trigger:
      exit_code: 0
    action:
      type: agent_message
      message: "Done"
""",
    )

    result = _load(tmp_path, rooms=[])

    assert result.automations == ()
    assert len(result.errors) == 1
    assert result.errors[0].field_path == ("automations", "missing_room", "action", "room")
    assert "room" in result.errors[0].message


def test_loader_returns_valid_sibling_and_structured_errors_for_entry_failures(tmp_path: Path) -> None:
    """One invalid automation entry should not discard valid siblings."""
    _write_automations(
        tmp_path,
        """
version: 1
automations:
  valid_probe:
    schedule: "*/1 * * * *"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
  bad_probe:
    schedule: "not cron"
    check:
      type: shell
      command: "true"
      timeout_seconds: 1
    action:
      type: none
""",
    )

    result = _load(tmp_path)

    assert [automation.automation_id for automation in result.automations] == ["valid_probe"]
    assert len(result.errors) == 1
    assert result.errors[0].automation_id == "bad_probe"
    assert result.errors[0].field_path == ("automations", "bad_probe", "schedule")
