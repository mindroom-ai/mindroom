"""Tests for the reversible agent rename plan/apply engine."""

from __future__ import annotations

import errno
import json
import os
import signal
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from mindroom.agent_rename import steps as steps_module
from mindroom.agent_rename.steps import (
    ApplyResult,
    DropPath,
    FileRewrite,
    FinalAction,
    MovePath,
    RenameApplyError,
    RenamePlan,
    RenameRefusedError,
    RenameUndoError,
    SqliteEdit,
    apply_plan,
)
from tests.agent_rename_helpers import tree_snapshot

NOW = datetime(2026, 10, 19, 21, 45, 58, tzinfo=UTC)
BACKUPS = "agent-rename-backups"


def _create_database(path: Path, *, wal: bool = False) -> None:
    with closing(sqlite3.connect(path)) as connection:
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE accounts (name TEXT PRIMARY KEY, user_id TEXT)")
        connection.execute("INSERT INTO accounts VALUES ('nova', '@old:x')")
        connection.commit()
    path.chmod(0o600)


def _rows(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT name, user_id FROM accounts ORDER BY name").fetchall()


def _set_user_id(connection: sqlite3.Connection) -> None:
    connection.execute("UPDATE accounts SET user_id = '@new:x' WHERE name = 'nova'")


def _fail_after_update(connection: sqlite3.Connection) -> None:
    _set_user_id(connection)
    msg = "boom"
    raise RuntimeError(msg)


def _rewrite_user_id(content: bytes) -> bytes:
    data = json.loads(content)
    data["user_id"] = "@new:x"
    return json.dumps(data).encode()


def _fail_rewrite(content: bytes) -> bytes:  # noqa: ARG001
    msg = "cannot rewrite"
    raise RuntimeError(msg)


def _without_backups(snapshot: dict[str, bytes | str]) -> dict[str, bytes | str]:
    return {key: value for key, value in snapshot.items() if key != BACKUPS and not key.startswith(f"{BACKUPS}/")}


def _make_storage(tmp_path: Path) -> Path:
    storage = tmp_path / "storage"
    (storage / "agents" / "old").mkdir(parents=True)
    (storage / "agents" / "old" / "state.txt").write_text("state")
    (storage / "accounts.json").write_text(json.dumps({"user_id": "@old:x"}))
    (storage / "accounts.json").chmod(0o640)
    _create_database(storage / "journal.db")
    (storage / "legacy.txt").write_text("legacy")
    return storage


def _four_step_plan(storage: Path) -> RenamePlan:
    return RenamePlan(
        storage_root=storage,
        steps=(
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            SqliteEdit(storage / "journal.db", _set_user_id, "update journal"),
            DropPath(storage / "legacy.txt", "drop legacy file"),
        ),
        reports=(),
    )


def test_apply_runs_steps_and_writes_manifest(tmp_path: Path) -> None:
    """Every step type takes effect, lands its backup in the backup dir, and is listed in the manifest."""
    storage = _make_storage(tmp_path)
    plan = _four_step_plan(storage)

    result = apply_plan(plan, kind="rename-account", now=NOW)

    assert isinstance(result, ApplyResult)
    assert result.warnings == ()
    assert result.backup_dir == storage / BACKUPS / "20261019T214558Z-rename-account"
    assert not (storage / "agents" / "old").exists()
    assert (storage / "agents" / "new" / "state.txt").read_text() == "state"
    assert json.loads((storage / "accounts.json").read_text()) == {"user_id": "@new:x"}
    assert (storage / "accounts.json").stat().st_mode & 0o777 == 0o640
    assert _rows(storage / "journal.db") == [("nova", "@new:x")]
    assert (storage / "journal.db").stat().st_mode & 0o777 == 0o600
    assert (result.backup_dir / "002" / "journal.db").stat().st_mode & 0o777 == 0o600
    assert not (storage / "legacy.txt").exists()
    assert (result.backup_dir / "003" / "legacy.txt").read_text() == "legacy"
    assert json.loads((result.backup_dir / "001" / "accounts.json").read_text()) == {"user_id": "@old:x"}
    assert _rows(result.backup_dir / "002" / "journal.db") == [("nova", "@old:x")]
    assert not list(storage.glob("**/*.tmp"))

    manifest = json.loads((result.backup_dir / "manifest.json").read_text())
    assert manifest["kind"] == "rename-account"
    assert manifest["created_at"] == NOW.isoformat()
    assert manifest["steps"] == [
        {
            "type": "MovePath",
            "description": "move agent dir",
            "source": str(storage / "agents" / "old"),
            "destination": str(storage / "agents" / "new"),
            "backup": None,
        },
        {
            "type": "FileRewrite",
            "description": "rewrite accounts",
            "source": str(storage / "accounts.json"),
            "destination": None,
            "backup": str(result.backup_dir / "001"),
        },
        {
            "type": "SqliteEdit",
            "description": "update journal",
            "source": str(storage / "journal.db"),
            "destination": None,
            "backup": str(result.backup_dir / "002"),
        },
        {
            "type": "DropPath",
            "description": "drop legacy file",
            "source": str(storage / "legacy.txt"),
            "destination": None,
            "backup": str(result.backup_dir / "003"),
        },
    ]


def test_backups_are_private_whatever_the_umask(tmp_path: Path) -> None:
    """Backups hold the Matrix password and OAuth stores, so only their owner may read them, even with umask 000."""
    storage = _make_storage(tmp_path)
    previous_umask = os.umask(0)
    try:
        result = apply_plan(_four_step_plan(storage), kind="rename-account", now=NOW)
    finally:
        os.umask(previous_umask)

    private_directories = [storage / BACKUPS, result.backup_dir, *(result.backup_dir / f"00{i}" for i in (1, 2, 3))]
    assert {path.name: path.stat().st_mode & 0o777 for path in private_directories} == {
        path.name: 0o700 for path in private_directories
    }
    assert (result.backup_dir / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_backup_dir_name_uses_utc(tmp_path: Path) -> None:
    """A timezone-aware timestamp is converted to UTC for the backup directory name."""
    plan = RenamePlan(storage_root=tmp_path, steps=(), reports=())

    result = apply_plan(plan, kind="rename", now=NOW.astimezone(timezone(timedelta(hours=2))))

    assert result.backup_dir == tmp_path / BACKUPS / "20261019T214558Z-rename"


def test_apply_refuses_an_existing_backup_dir(tmp_path: Path) -> None:
    """Two applies in the same second never share or overwrite a backup directory."""
    plan = RenamePlan(storage_root=tmp_path, steps=(), reports=())
    apply_plan(plan, kind="rename-account", now=NOW)

    with pytest.raises(RenameRefusedError, match="already exists"):
        apply_plan(plan, kind="rename-account", now=NOW)


def test_failure_undoes_completed_steps(tmp_path: Path) -> None:
    """A failing third step restores the original tree and names the step."""
    storage = _make_storage(tmp_path)
    original = tree_snapshot(storage)
    plan = RenamePlan(
        storage_root=storage,
        steps=(
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            SqliteEdit(storage / "journal.db", _fail_after_update, "update journal"),
        ),
        reports=(),
    )

    with pytest.raises(RenameApplyError, match="update journal") as caught:
        apply_plan(plan, kind="rename-account", now=NOW)

    assert caught.value.step == "update journal"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert _without_backups(tree_snapshot(storage)) == original
    assert (storage / "accounts.json").stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("wal", [False, True])
def test_failure_undoes_every_step_kind(tmp_path: Path, wal: bool) -> None:
    """Moves, rewrites, committed database edits, and drops are all reversed when a later step fails."""
    storage = _make_storage(tmp_path)
    if wal:
        (storage / "journal.db").unlink()
        _create_database(storage / "journal.db", wal=True)
    # The SQLite backup API restores the same content but not the same header bytes, so compare rows instead.
    original = tree_snapshot(storage)
    original.pop("journal.db")

    def leave_stale_wal_files_then_fail(content: bytes) -> bytes:  # noqa: ARG001
        # Closing the last connection already removes these, so a later step has to recreate them.
        (storage / "journal.db-wal").write_bytes(b"stale wal")
        (storage / "journal.db-shm").write_bytes(b"stale shm")
        msg = "boom"
        raise RuntimeError(msg)

    plan = RenamePlan(
        storage_root=storage,
        steps=(
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            SqliteEdit(storage / "journal.db", _set_user_id, "update journal"),
            DropPath(storage / "legacy.txt", "drop legacy file"),
            FileRewrite(storage / "agents" / "new" / "state.txt", leave_stale_wal_files_then_fail, "rewrite state"),
        ),
        reports=(),
    )

    with pytest.raises(RenameApplyError) as caught:
        apply_plan(plan, kind="rename-account", now=NOW)

    assert caught.value.step == "rewrite state"
    assert not list(storage.glob("journal.db-*"))
    assert _rows(storage / "journal.db") == [("nova", "@old:x")]
    assert (storage / "journal.db").stat().st_mode & 0o777 == 0o600
    restored = _without_backups(tree_snapshot(storage))
    assert restored.pop("journal.db")
    assert restored == original


@pytest.mark.parametrize("first_failing_write", [2, 3, 4])
def test_manifest_write_failure_still_undoes_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_failing_write: int,
) -> None:
    """A manifest that cannot be written, whether after a step or after a failure, never strands applied steps."""
    storage = _make_storage(tmp_path)
    original = tree_snapshot(storage)
    original.pop("journal.db")
    plan = RenamePlan(
        storage_root=storage,
        steps=(
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "legacy.txt", _fail_rewrite, "rewrite legacy"),
        ),
        reports=(),
    )
    # Write 1 precedes the first step, writes 2 and 3 follow steps 1 and 2, and write 4 follows the failure.
    real_write_manifest = steps_module._write_manifest
    writes: list[None] = []

    def write_manifest(*args: object, **kwargs: object) -> None:
        writes.append(None)
        if len(writes) >= first_failing_write:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write_manifest(*args, **kwargs)

    monkeypatch.setattr(steps_module, "_write_manifest", write_manifest)

    with pytest.raises(RenameApplyError) as caught:
        apply_plan(plan, kind="rename-account", now=NOW)

    expected_step = {2: "rewrite accounts", 3: "move agent dir", 4: "rewrite legacy"}[first_failing_write]
    assert caught.value.step == expected_step
    restored = _without_backups(tree_snapshot(storage))
    restored.pop("journal.db")
    assert restored == original


def test_keyboard_interrupt_undoes_completed_steps(tmp_path: Path) -> None:
    """An interrupt during a step undoes the completed steps and then propagates unchanged."""
    storage = _make_storage(tmp_path)
    original = tree_snapshot(storage)
    original.pop("journal.db")

    def interrupt(content: bytes) -> bytes:  # noqa: ARG001
        raise KeyboardInterrupt

    plan = RenamePlan(
        storage_root=storage,
        steps=(
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            FileRewrite(storage / "legacy.txt", interrupt, "rewrite legacy"),
        ),
        reports=(),
    )

    with pytest.raises(KeyboardInterrupt):
        apply_plan(plan, kind="rename-account", now=NOW)

    restored = _without_backups(tree_snapshot(storage))
    restored.pop("journal.db")
    assert restored == original


def test_interrupt_during_undo_is_an_undo_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C is ignored while undoing, and an interrupt that still escapes an undo becomes ``RenameUndoError``.

    Undoing the journal copies the whole database back, so an operator may press Ctrl-C again;
    the command must then still name the manifest and say not to start MindRoom.
    """
    storage = _make_storage(tmp_path)
    handlers_during_undo: list[object] = []

    def interrupted_undo(self: MovePath, backup: Path) -> None:  # noqa: ARG001
        handlers_during_undo.append(signal.getsignal(signal.SIGINT))
        raise KeyboardInterrupt

    monkeypatch.setattr(MovePath, "_undo", interrupted_undo)
    plan = RenamePlan(
        storage_root=storage,
        steps=(
            MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "accounts.json", _fail_rewrite, "rewrite accounts"),
        ),
        reports=(),
    )
    handler_before = signal.getsignal(signal.SIGINT)

    with pytest.raises(RenameUndoError, match=r"undoing move agent dir failed \(KeyboardInterrupt\)") as caught:
        apply_plan(plan, kind="rename-account", now=NOW)

    assert caught.value.manifest == storage / BACKUPS / "20261019T214558Z-rename-account" / "manifest.json"
    assert isinstance(caught.value.__cause__, KeyboardInterrupt)
    assert handlers_during_undo == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGINT) is handler_before


def test_move_refuses_existing_destination(tmp_path: Path) -> None:
    """A move onto an existing path refuses and leaves both paths and earlier steps untouched."""
    storage = _make_storage(tmp_path)
    (storage / "agents" / "new").mkdir()
    (storage / "agents" / "new" / "other.txt").write_text("other")
    original = tree_snapshot(storage)
    step = MovePath(storage / "agents" / "old", storage / "agents" / "new", "move agent dir")

    with pytest.raises(RenameRefusedError, match="already exists"):
        step.apply(tmp_path / "unused-backup")
    assert tree_snapshot(storage) == original

    plan = RenamePlan(
        storage_root=storage,
        steps=(FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"), step),
        reports=(),
    )
    with pytest.raises(RenameRefusedError, match="already exists"):
        apply_plan(plan, kind="rename-account", now=NOW)
    assert _without_backups(tree_snapshot(storage)) == original


def test_move_refuses_a_dangling_symlink_destination(tmp_path: Path) -> None:
    """A broken symlink still occupies the destination, so the move refuses."""
    source = tmp_path / "source"
    source.mkdir()
    destination = tmp_path / "destination"
    destination.symlink_to(tmp_path / "missing")

    with pytest.raises(RenameRefusedError, match="already exists"):
        MovePath(source, destination, "move").apply(tmp_path / "unused-backup")

    assert source.is_dir()
    assert destination.is_symlink()


def test_final_action_failure_is_a_warning(tmp_path: Path) -> None:
    """A failing final action is reported as a warning and earlier steps stay applied."""
    storage = _make_storage(tmp_path)
    calls: list[str] = []

    def fail() -> None:
        calls.append("fail")
        msg = "cannot reload"
        raise RuntimeError(msg)

    plan = RenamePlan(
        storage_root=storage,
        steps=(
            FinalAction(fail, "reload the runtime"),
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            FinalAction(lambda: calls.append("second"), "second action"),
        ),
        reports=(),
    )

    result = apply_plan(plan, kind="rename-account", now=NOW)

    assert json.loads((storage / "accounts.json").read_text()) == {"user_id": "@new:x"}
    assert calls == ["fail", "second"]
    assert len(result.warnings) == 1
    assert "reload the runtime" in result.warnings[0]
    assert "cannot reload" in result.warnings[0]


def test_interrupted_final_action_is_a_warning(tmp_path: Path) -> None:
    """Ctrl-C during a final action comes after every reversible step, so apply still returns its result.

    The interrupted action and the ones it kept from running become warnings, and nothing is undone.
    """
    storage = _make_storage(tmp_path)
    calls: list[str] = []

    def interrupted() -> None:
        calls.append("interrupted")
        raise KeyboardInterrupt

    plan = RenamePlan(
        storage_root=storage,
        steps=(
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            FinalAction(interrupted, "retire the old worker"),
            FinalAction(lambda: calls.append("second"), "retire another worker"),
        ),
        reports=(),
    )

    result = apply_plan(plan, kind="rename-account", now=NOW)

    assert json.loads((storage / "accounts.json").read_text()) == {"user_id": "@new:x"}
    assert calls == ["interrupted"]
    assert result.warnings == (
        "retire the old worker: interrupted",
        "retire another worker: not run after the interrupt",
    )


def test_final_actions_run_after_every_other_step(tmp_path: Path) -> None:
    """A final action listed first still runs after the reversible steps, and never when one fails."""
    storage = _make_storage(tmp_path)
    seen: list[bytes] = []
    accounts = storage / "accounts.json"
    final = FinalAction(lambda: seen.append(accounts.read_bytes()), "observe accounts")

    apply_plan(
        RenamePlan(storage, (final, FileRewrite(accounts, _rewrite_user_id, "rewrite accounts")), ()),
        kind="rename-account",
        now=NOW,
    )
    assert json.loads(seen[0]) == {"user_id": "@new:x"}

    seen.clear()
    failing = RenamePlan(
        storage,
        (final, FileRewrite(accounts, _fail_rewrite, "rewrite accounts")),
        (),
    )
    with pytest.raises(RenameApplyError):
        apply_plan(failing, kind="rename-account", now=NOW + timedelta(seconds=1))
    assert seen == []


def test_failed_undo_raises_with_the_manifest(tmp_path: Path) -> None:
    """When undo itself fails, the manifest path is reported and no further undo is attempted."""
    storage = _make_storage(tmp_path)
    source = storage / "agents" / "old"

    def occupy_source_then_fail(content: bytes) -> bytes:  # noqa: ARG001
        source.mkdir()
        msg = "boom"
        raise RuntimeError(msg)

    plan = RenamePlan(
        storage_root=storage,
        steps=(
            FileRewrite(storage / "accounts.json", _rewrite_user_id, "rewrite accounts"),
            MovePath(source, storage / "agents" / "new", "move agent dir"),
            FileRewrite(storage / "legacy.txt", occupy_source_then_fail, "rewrite legacy"),
        ),
        reports=(),
    )

    with pytest.raises(RenameUndoError, match="move agent dir") as caught:
        apply_plan(plan, kind="rename-account", now=NOW)

    manifest = storage / BACKUPS / "20261019T214558Z-rename-account" / "manifest.json"
    assert caught.value.manifest == manifest
    assert len(json.loads(manifest.read_text())["steps"]) == 3
    assert json.loads((storage / "accounts.json").read_text()) == {"user_id": "@new:x"}


def test_sqlite_edit_does_not_create_a_missing_database(tmp_path: Path) -> None:
    """A missing database fails the step instead of creating an empty one."""
    plan = RenamePlan(
        storage_root=tmp_path,
        steps=(SqliteEdit(tmp_path / "missing.db", _set_user_id, "update journal"),),
        reports=(),
    )

    with pytest.raises(RenameApplyError, match="update journal"):
        apply_plan(plan, kind="rename-account", now=NOW)

    assert not (tmp_path / "missing.db").exists()


def test_describe_lists_steps_then_reports() -> None:
    """Describing a plan lists step descriptions first and prefixed reports last without touching disk."""
    missing = Path("/nonexistent/agent-rename")
    plan = RenamePlan(
        storage_root=missing,
        steps=(
            MovePath(missing / "a", missing / "b", "move a to b"),
            FileRewrite(missing / "c.json", _rewrite_user_id, "rewrite c"),
            SqliteEdit(missing / "d.db", _set_user_id, "update d"),
            DropPath(missing / "e", "drop e"),
            FinalAction(lambda: None, "final action"),
        ),
        reports=("legacy file x is not read by any code",),
    )

    assert plan.describe() == [
        "move a to b",
        "rewrite c",
        "update d",
        "drop e",
        "final action",
        "report: legacy file x is not read by any code",
    ]
    assert not missing.exists()


def test_describe_leaves_the_filesystem_untouched(tmp_path: Path) -> None:
    """Building and describing a plan changes nothing on disk."""
    storage = _make_storage(tmp_path)
    before = tree_snapshot(tmp_path)

    _four_step_plan(storage).describe()

    assert tree_snapshot(tmp_path) == before
