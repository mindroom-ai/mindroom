"""Conversation mode choices retain canonical storage and agent boundaries."""

# ruff: noqa: D103

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mindroom import agent_modes


def test_default_mode_read_does_not_create_state(tmp_path: Path) -> None:
    state_root = tmp_path / "unused-agent"
    assert agent_modes.resolve_agent_mode(state_root, "agent", "session") == "standard"
    assert not state_root.exists()


def test_saved_mode_read_does_not_require_writable_storage(tmp_path: Path) -> None:
    agent_modes.set_agent_mode(tmp_path, "agent", "session", "minimal", "alice")
    lock_path = tmp_path / "agent_modes.lock"
    lock_path.unlink()
    # A directory here rejects even root's attempts to open a new lock file.
    lock_path.mkdir()
    assert agent_modes.resolve_agent_mode(tmp_path, "agent", "session") == "minimal"


def test_mode_override_scope_and_reset(tmp_path: Path) -> None:
    agent_modes.set_agent_mode(tmp_path, "assistant", "room-session", "minimal", "@alice:test")
    assert agent_modes.resolve_agent_mode(tmp_path, "assistant", "room-session") == "minimal"
    assert agent_modes.resolve_agent_mode(tmp_path, "researcher", "room-session") == "standard"
    assert agent_modes.resolve_agent_mode(tmp_path, "assistant", "thread-session") == "standard"
    assert agent_modes.resolve_agent_mode(tmp_path / "private", "assistant", "room-session") == "standard"
    assert agent_modes.clear_agent_mode(tmp_path, "assistant", "room-session")
    assert not agent_modes.clear_agent_mode(tmp_path, "assistant", "room-session")
    assert agent_modes.resolve_agent_mode(tmp_path, "assistant", "room-session") == "standard"


def test_concurrent_choices_preserve_other_sessions(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda number: agent_modes.set_agent_mode(tmp_path, "agent", str(number), "minimal", "alice"),
                range(40),
            ),
        )
    assert all(agent_modes.resolve_agent_mode(tmp_path, "agent", str(number)) == "minimal" for number in range(40))


def test_corrupt_mode_records_fail_to_standard(tmp_path: Path) -> None:
    agent_modes.set_agent_mode(tmp_path, "agent", "session", "minimal", "alice")
    path = next(tmp_path.rglob("*.json"))
    path.write_text("{broken", encoding="utf-8")
    assert agent_modes.resolve_agent_mode(tmp_path, "agent", "session") == "standard"
    agent_modes.set_agent_mode(tmp_path, "agent", "session", "standard", "alice")
    assert agent_modes.resolve_agent_mode(tmp_path, "agent", "session") == "standard"


def test_mode_records_are_bounded(tmp_path: Path) -> None:
    for number in range(1001):
        agent_modes.set_agent_mode(tmp_path, "agent", str(number), "minimal", "alice")
    assert agent_modes.resolve_agent_mode(tmp_path, "agent", "0") == "standard"
    assert agent_modes.resolve_agent_mode(tmp_path, "agent", "1000") == "minimal"
