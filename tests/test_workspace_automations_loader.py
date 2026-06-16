"""Tests for workspace automation YAML loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.models import WorkspaceAutomationPolicyConfig
from mindroom.workspace_automations.loader import load_workspace_automations

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.workspace_automations.models import WorkspaceAutomationLoadResult


def _policy(
    *,
    enabled: bool = True,
    max_timeout_seconds: int = 30,
    allowed_actions: list[str] | None = None,
) -> WorkspaceAutomationPolicyConfig:
    return WorkspaceAutomationPolicyConfig(
        enabled=enabled,
        max_timeout_seconds=max_timeout_seconds,
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
