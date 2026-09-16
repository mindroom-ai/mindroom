"""Persistent browser profile lock recovery contracts."""

import os
from pathlib import Path

import pytest

from mindroom.browser_profile import clear_stale_singleton_locks


@pytest.mark.parametrize("name", ["SingletonLock", "SingletonCookie", "SingletonSocket"])
def test_clear_stale_singleton_locks_unlinks_stale_symlink(tmp_path: Path, name: str) -> None:
    """Stale Chromium singleton lock symlinks should be removed."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    lock = profile_dir / name
    target = tmp_path / "mindroom-999999999"
    target.write_text("preserve target")
    lock.symlink_to(target)

    clear_stale_singleton_locks(profile_dir)

    assert not lock.is_symlink()
    assert target.read_text() == "preserve target"


def test_clear_stale_singleton_locks_keeps_live_pid_symlink(tmp_path: Path) -> None:
    """Live Chromium singleton lock symlinks should be left in place."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    lock = profile_dir / "SingletonLock"
    lock.symlink_to(f"mindroom-{os.getpid()}")

    clear_stale_singleton_locks(profile_dir)

    assert lock.is_symlink()


def test_clear_stale_singleton_locks_is_idempotent_for_empty_dir(tmp_path: Path) -> None:
    """The exported singleton-lock cleanup helper should be safe for empty profiles."""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    clear_stale_singleton_locks(profile_dir)
    clear_stale_singleton_locks(profile_dir)

    assert list(profile_dir.iterdir()) == []


@pytest.mark.parametrize("name", ["SingletonLock", "SingletonCookie", "SingletonSocket"])
@pytest.mark.parametrize("target", ["123456789", "unknown-owner", "host-", "host-1-tail", "/missing/SingletonSocket"])
def test_clear_stale_singleton_locks_keeps_unparseable_links(tmp_path: Path, name: str, target: str) -> None:
    """No ownership evidence means no unlink, even for a dangling link."""
    lock = tmp_path / name
    lock.symlink_to(target)

    clear_stale_singleton_locks(tmp_path)

    assert lock.readlink() == Path(target)


def test_clear_stale_singleton_locks_preserves_other_profile_entries(tmp_path: Path) -> None:
    """Ordinary files, directories and unrelated links are never cleanup candidates."""
    (tmp_path / "SingletonLock").write_text("host-999999999")
    (tmp_path / "SingletonCookie").mkdir()
    (tmp_path / "SingletonSocket").write_bytes(b"socket data")
    (tmp_path / "OtherLock").symlink_to("host-999999999")

    clear_stale_singleton_locks(tmp_path)

    assert (tmp_path / "SingletonLock").read_text() == "host-999999999"
    assert (tmp_path / "SingletonCookie").is_dir()
    assert (tmp_path / "SingletonSocket").read_bytes() == b"socket data"
    assert (tmp_path / "OtherLock").readlink() == Path("host-999999999")


def test_clear_stale_singleton_locks_keeps_permission_denied_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure to inspect an owner must retain its lock and allow startup to proceed."""
    lock = tmp_path / "SingletonLock"
    lock.symlink_to("host-123")

    def deny_inspection(pid: int, signal: int) -> None:
        assert (pid, signal) == (123, 0)
        raise PermissionError

    monkeypatch.setattr(os, "kill", deny_inspection)

    clear_stale_singleton_locks(tmp_path)

    assert lock.readlink() == Path("host-123")
