"""Conversation mode choices retain canonical storage and agent boundaries."""

# ruff: noqa: D103

import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mindroom import agent_modes
from mindroom.constants import RuntimePaths, resolve_runtime_paths


def _paths(tmp_path: Path) -> tuple[RuntimePaths, Path]:
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    return runtime_paths, runtime_paths.storage_root / "agents" / "agent"


def test_default_mode_read_does_not_create_state(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "session") == "standard"
    assert not (tmp_path / "agents").exists()
    assert not (tmp_path / "tracking").exists()


def test_saved_mode_read_does_not_require_writable_storage(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    agent_modes.set_agent_mode(paths, state_root, "agent", "session", "minimal", "alice")
    lock_path = tmp_path / "tracking" / "agents" / "agent" / "agent_modes.lock"
    lock_path.unlink()
    # A directory here rejects even root's attempts to open a new lock file.
    lock_path.mkdir()
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "session") == "minimal"


def test_choices_stay_out_of_worker_mounted_state_roots(tmp_path: Path) -> None:
    """A lock worker code holds in the state root cannot stall a mode change in the primary."""
    paths, state_root = _paths(tmp_path)
    state_root.mkdir(parents=True)
    with (state_root / "agent_modes.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        change = threading.Thread(
            target=agent_modes.set_agent_mode,
            args=(paths, state_root, "agent", "session", "minimal", "alice"),
            daemon=True,
        )
        change.start()
        change.join(timeout=5)
        stalled = change.is_alive()
    change.join()
    assert not stalled
    (state_root / "agent_modes.json").write_text("{}", encoding="utf-8")
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "session") == "minimal"
    assert {path.name for path in state_root.iterdir()} == {"agent_modes.json", "agent_modes.lock"}


def test_mode_override_scope_and_reset(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    private_root = tmp_path / "private_instances" / "scope" / "assistant"
    agent_modes.set_agent_mode(paths, state_root, "assistant", "room-session", "minimal", "@alice:test")
    assert agent_modes.resolve_agent_mode(paths, state_root, "assistant", "room-session") == "minimal"
    assert agent_modes.resolve_agent_mode(paths, state_root, "researcher", "room-session") == "standard"
    assert agent_modes.resolve_agent_mode(paths, state_root, "assistant", "thread-session") == "standard"
    assert agent_modes.resolve_agent_mode(paths, private_root, "assistant", "room-session") == "standard"
    assert agent_modes.clear_agent_mode(paths, state_root, "assistant", "room-session")
    assert not agent_modes.clear_agent_mode(paths, state_root, "assistant", "room-session")
    assert agent_modes.resolve_agent_mode(paths, state_root, "assistant", "room-session") == "standard"


def test_concurrent_choices_preserve_other_sessions(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda number: agent_modes.set_agent_mode(paths, state_root, "agent", str(number), "minimal", "alice"),
                range(40),
            ),
        )
    assert all(
        agent_modes.resolve_agent_mode(paths, state_root, "agent", str(number)) == "minimal" for number in range(40)
    )


def test_corrupt_mode_records_fail_to_standard(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    agent_modes.set_agent_mode(paths, state_root, "agent", "session", "minimal", "alice")
    path = next(tmp_path.rglob("*.json"))
    path.write_text("{broken", encoding="utf-8")
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "session") == "standard"
    agent_modes.set_agent_mode(paths, state_root, "agent", "session", "standard", "alice")
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "session") == "standard"


def test_mode_records_are_bounded(tmp_path: Path) -> None:
    paths, state_root = _paths(tmp_path)
    for number in range(1001):
        agent_modes.set_agent_mode(paths, state_root, "agent", str(number), "minimal", "alice")
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "0") == "standard"
    assert agent_modes.resolve_agent_mode(paths, state_root, "agent", "1000") == "minimal"
