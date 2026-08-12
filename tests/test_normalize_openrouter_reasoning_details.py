"""Tests for the one-shot OpenRouter reasoning-details database normalizer."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.normalize_openrouter_reasoning_details as normalizer
from scripts.normalize_openrouter_reasoning_details import normalize_database

SCRIPT = Path(__file__).parents[1] / "scripts" / "normalize_openrouter_reasoning_details.py"


def _polluted_runs() -> list[dict[str, Any]]:
    return [
        {
            "run_id": "run-1",
            "messages": [
                {
                    "role": "assistant",
                    "content": "answer",
                    "provider_data": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "index": 0, "text": "one "},
                            {"type": "reasoning.text", "index": 0, "text": "two"},
                            {
                                "type": "reasoning.text",
                                "index": 0,
                                "text": " signed",
                                "signature": "sig",
                            },
                            {"type": "reasoning.text", "index": 0, "text": " tail"},
                        ],
                        "kept": {"nested": True},
                    },
                },
                {"role": "user", "content": "unchanged"},
            ],
            "model_provider_data": {
                "reasoning_details": [
                    {"type": "reasoning.text", "id": "reasoning-1", "text": "model "},
                    {"type": "reasoning.text", "id": "reasoning-1", "text": "data"},
                ],
                "other": [1, 2, 3],
            },
            "events": [
                {
                    "payload": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "index": 4, "text": "event "},
                            {"type": "reasoning.text", "index": 4, "text": "details"},
                        ],
                    },
                },
            ],
        },
        {
            "run_id": "run-2",
            "reasoning_details": "malformed-is-untouched",
            "events": [
                {
                    "reasoning_details": [
                        {"type": "reasoning.text", "index": 9, "text": 42},
                        {"type": "reasoning.text", "index": 9, "text": "not-merged"},
                        {"type": "reasoning.summary", "text": "kept"},
                    ],
                },
            ],
        },
    ]


def _expected_runs() -> list[dict[str, Any]]:
    expected = _polluted_runs()
    expected[0]["messages"][0]["provider_data"]["reasoning_details"] = [
        {"type": "reasoning.text", "index": 0, "text": "one two"},
        {
            "type": "reasoning.text",
            "index": 0,
            "text": " signed",
            "signature": "sig",
        },
        {"type": "reasoning.text", "index": 0, "text": " tail"},
    ]
    expected[0]["model_provider_data"]["reasoning_details"] = [
        {"type": "reasoning.text", "id": "reasoning-1", "text": "model data"},
    ]
    expected[0]["events"][0]["payload"]["reasoning_details"] = [
        {"type": "reasoning.text", "index": 4, "text": "event details"},
    ]
    return expected


def _encode_runs(runs: list[dict[str, Any]]) -> str:
    """Match Mom's JSON column containing a JSON-encoded runs string."""
    return json.dumps(json.dumps(runs, separators=(",", ":")))


def _decode_runs(value: str) -> list[dict[str, Any]]:
    encoded_runs = json.loads(value)
    assert isinstance(encoded_runs, str)
    runs = json.loads(encoded_runs)
    assert isinstance(runs, list)
    return runs


@pytest.fixture
def sessions_db(tmp_path: Path) -> tuple[Path, dict[str, list[dict[str, Any]]]]:
    """Create the same double-encoded runs storage shape used by Mom."""
    db_path = tmp_path / "mind.db"
    source_rows = {
        "session-a": _polluted_runs(),
        "session-b": [
            {
                "run_id": "run-3",
                "messages": [
                    {
                        "role": "assistant",
                        "provider_data": {
                            "reasoning_details": [
                                {"type": "reasoning.text", "index": 2, "text": "already single"},
                            ],
                        },
                    },
                ],
                "unrelated": {"value": None},
            },
        ],
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE mind_sessions (session_id TEXT PRIMARY KEY, runs JSON NOT NULL)")
        connection.executemany(
            "INSERT INTO mind_sessions(session_id, runs) VALUES (?, ?)",
            [(session_id, _encode_runs(runs)) for session_id, runs in source_rows.items()],
        )
    return db_path, source_rows


def _rows(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    with sqlite3.connect(db_path) as connection:
        return {
            session_id: _decode_runs(runs)
            for session_id, runs in connection.execute(
                "SELECT session_id, runs FROM mind_sessions ORDER BY session_id",
            )
        }


def _run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(db_path), "--table", "mind_sessions", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _result(stdout: str) -> dict[str, Any]:
    return json.loads(stdout.strip().splitlines()[-1])


def test_dry_run_is_byte_identical_and_reports_recursive_changes(
    sessions_db: tuple[Path, dict[str, list[dict[str, Any]]]],
) -> None:
    """Dry-run reports every nested location without touching database bytes."""
    db_path, source_rows = sessions_db
    before = hashlib.sha256(db_path.read_bytes()).digest()

    completed = _run_cli(db_path, "--dry-run")

    assert completed.returncode == 0, completed.stderr
    assert hashlib.sha256(db_path.read_bytes()).digest() == before
    assert _rows(db_path) == source_rows
    result = _result(completed.stdout)
    assert result["mode"] == "dry-run"
    assert result["rows"] == 2
    assert result["changed_rows"] == 1
    assert result["reasoning_details_before"] == 12
    assert result["reasoning_details_after"] == 9
    assert result["backup_path"] is None


def test_apply_writes_exact_post_images_and_preserves_invariants(
    sessions_db: tuple[Path, dict[str, list[dict[str, Any]]]],
) -> None:
    """Apply writes only the hand-authored expected recursive normalization."""
    db_path, source_rows = sessions_db

    completed = _run_cli(db_path, "--apply")

    assert completed.returncode == 0, completed.stderr
    assert _rows(db_path) == {
        "session-a": _expected_runs(),
        "session-b": source_rows["session-b"],
    }
    result = _result(completed.stdout)
    assert result["mode"] == "apply"
    assert result["changed_rows"] == 1
    assert result["runs"] == 3
    assert result["reasoning_text_sha256_before"] == result["reasoning_text_sha256_after"]
    assert result["integrity_check"] == "ok"


def test_apply_backup_is_valid_and_emitted_restore_command_restores_preimage(
    sessions_db: tuple[Path, dict[str, list[dict[str, Any]]]],
) -> None:
    """The SQLite backup and emitted command restore the exact logical pre-image."""
    db_path, source_rows = sessions_db
    completed = _run_cli(db_path, "--apply")
    assert completed.returncode == 0, completed.stderr
    result = _result(completed.stdout)
    backup_path = Path(result["backup_path"])

    assert backup_path.exists()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert _rows(backup_path) == source_rows
    assert shlex.split(result["restore_command"])[0:2] == [sys.executable, "-c"]
    stale_sidecars = [Path(f"{db_path}-wal"), Path(f"{db_path}-shm")]
    for sidecar in stale_sidecars:
        sidecar.write_bytes(b"stale")

    restored = subprocess.run(
        shlex.split(result["restore_command"]),
        check=False,
        capture_output=True,
        text=True,
    )

    assert restored.returncode == 0, restored.stderr
    assert _rows(db_path) == source_rows
    assert not any(sidecar.exists() for sidecar in stale_sidecars)
    assert list(db_path.parent.glob(".mind.db.restore-*")) == []


def test_post_commit_error_carries_verified_recovery_and_restores_preimage(
    sessions_db: tuple[Path, dict[str, list[dict[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verification error after commit names the backup and exact recovery command."""
    db_path, source_rows = sessions_db
    real_integrity_check = normalizer._integrity_check
    checks = 0

    def fail_post_commit_check(connection: sqlite3.Connection, label: str) -> None:
        nonlocal checks
        checks += 1
        real_integrity_check(connection, label)
        if checks == 4:
            msg = "injected post-commit verification failure"
            raise normalizer.NormalizationError(msg)

    monkeypatch.setattr(normalizer, "_integrity_check", fail_post_commit_check)

    with pytest.raises(normalizer.NormalizationError, match="injected") as caught:
        normalize_database(db_path, table="mind_sessions", apply=True)

    assert caught.value.backup_path is not None
    assert caught.value.restore_command is not None
    assert caught.value.backup_path.exists()
    restored = subprocess.run(
        shlex.split(caught.value.restore_command),
        check=False,
        capture_output=True,
        text=True,
    )
    assert restored.returncode == 0, restored.stderr
    assert _rows(db_path) == source_rows


def test_restore_staging_failure_keeps_live_database_intact(tmp_path: Path) -> None:
    """A failed staging copy must not unlink or replace the live database."""
    database_path = tmp_path / "mind.db"
    original = b"live database bytes"
    database_path.write_bytes(original)
    missing_backup = tmp_path / "missing-backup.db"
    restore_command = normalizer._restore_command(missing_backup, database_path)

    restored = subprocess.run(
        shlex.split(restore_command),
        check=False,
        capture_output=True,
        text=True,
    )

    assert restored.returncode != 0
    assert database_path.read_bytes() == original
    assert list(tmp_path.glob(".mind.db.restore-*")) == []


def test_restore_replace_failure_keeps_main_and_sidecars_intact(tmp_path: Path) -> None:
    """A failed atomic replace must leave the live main DB and sidecars untouched."""
    database_path = tmp_path / "mind.db"
    backup_path = tmp_path / "backup.db"
    original_files = {
        database_path: b"live database bytes",
        Path(f"{database_path}-wal"): b"live wal bytes",
        Path(f"{database_path}-shm"): b"live shm bytes",
    }
    for path, contents in original_files.items():
        path.write_bytes(contents)
    backup_path.write_bytes(b"verified backup bytes")
    command = shlex.split(normalizer._restore_command(backup_path, database_path))
    replace_line = "    os.replace(temporary, destination)\n"
    assert replace_line in command[2]
    command[2] = command[2].replace(
        replace_line,
        "    real_replace = os.replace\n"
        "    os.replace = lambda candidate, target: (_ for _ in ()).throw(OSError('injected replace failure')) "
        "if candidate == temporary and target == destination else real_replace(candidate, target)\n"
        "    os.replace(temporary, destination)\n",
    )

    restored = subprocess.run(command, check=False, capture_output=True, text=True)

    assert restored.returncode != 0
    assert {path: path.read_bytes() for path in original_files} == original_files
    assert list(tmp_path.glob(".mind.db.restore-*")) == []


def test_restore_main_replace_window_has_no_recognized_sqlite_sidecars(tmp_path: Path) -> None:
    """After main replacement, old WAL/SHM must exist only under quarantine names."""
    database_path = tmp_path / "mind.db"
    backup_path = tmp_path / "backup.db"
    database_path.write_bytes(b"old main")
    backup_path.write_bytes(b"new main")
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    wal_path.write_bytes(b"valid old wal")
    shm_path.write_bytes(b"valid old shm")
    command = shlex.split(normalizer._restore_command(backup_path, database_path))
    replace_line = "    os.replace(temporary, destination)\n"
    assert replace_line in command[2]
    command[2] = command[2].replace(
        replace_line,
        replace_line
        + "    assert not os.path.exists(destination + '-wal')\n"
        + "    assert not os.path.exists(destination + '-shm')\n"
        + "    raise SystemExit(75)\n",
    )

    interrupted = subprocess.run(command, check=False, capture_output=True, text=True)

    assert interrupted.returncode == 75, interrupted.stderr
    assert database_path.read_bytes() == b"new main"
    assert not wal_path.exists()
    assert not shm_path.exists()
    quarantines = list(tmp_path.glob(".mind.db.restore-*.quarantine-*"))
    assert sorted(path.read_bytes() for path in quarantines) == [b"valid old shm", b"valid old wal"]


def test_restore_rollback_failure_retains_unrecognized_quarantine(tmp_path: Path) -> None:
    """Failed sidecar rollback retains recoverable bytes under an unrecognized name."""
    database_path = tmp_path / "mind.db"
    backup_path = tmp_path / "backup.db"
    database_path.write_bytes(b"old main")
    backup_path.write_bytes(b"new main")
    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    wal_path.write_bytes(b"old wal")
    shm_path.write_bytes(b"old shm")
    command = shlex.split(normalizer._restore_command(backup_path, database_path))
    replace_line = "    os.replace(temporary, destination)\n"
    assert replace_line in command[2]
    injection = (
        "    real_replace = os.replace\n"
        "    def injected_replace(candidate, target):\n"
        "        if candidate == temporary and target == destination:\n"
        "            raise OSError('injected main replace failure')\n"
        "        if candidate.endswith('.quarantine-wal'):\n"
        "            raise OSError('injected WAL rollback failure')\n"
        "        return real_replace(candidate, target)\n"
        "    os.replace = injected_replace\n" + replace_line
    )
    command[2] = command[2].replace(replace_line, injection)

    failed = subprocess.run(command, check=False, capture_output=True, text=True)

    assert failed.returncode != 0
    assert "retained quarantine paths" in failed.stderr
    assert database_path.read_bytes() == b"old main"
    assert not wal_path.exists()
    assert shm_path.read_bytes() == b"old shm"
    quarantines = list(tmp_path.glob(".mind.db.restore-*.quarantine-wal"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == b"old wal"


def test_session_rows_are_streamed_without_fetchall(  # noqa: C901 -- proxy deliberately models cursor protocol
    sessions_db: tuple[Path, dict[str, list[dict[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Large runs rows must be consumed incrementally instead of eagerly materialized."""
    db_path, _ = sessions_db
    real_connect = sqlite3.connect

    class CursorProxy:
        def __init__(self, cursor: sqlite3.Cursor, *, session_rows: bool) -> None:
            self._cursor = cursor
            self._session_rows = session_rows

        def fetchall(self) -> list[tuple[object, ...]]:
            if self._session_rows:
                msg = "session rows must not use fetchall"
                raise AssertionError(msg)
            return self._cursor.fetchall()

        def fetchone(self) -> tuple[object, ...] | None:
            return self._cursor.fetchone()

        def __iter__(self) -> CursorProxy:
            return self

        def __next__(self) -> tuple[object, ...]:
            row = self._cursor.fetchone()
            if row is None:
                raise StopIteration
            return row

    class ConnectionProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> CursorProxy:
            cursor = self._connection.execute(sql, parameters)
            normalized_sql = " ".join(sql.lower().split())
            return CursorProxy(
                cursor,
                session_rows=normalized_sql.startswith("select session_id, runs from"),
            )

        @property
        def in_transaction(self) -> bool:
            return self._connection.in_transaction

        def close(self) -> None:
            self._connection.close()

    def streaming_connect(database: str | Path) -> ConnectionProxy:
        return ConnectionProxy(real_connect(database))

    monkeypatch.setattr(normalizer.sqlite3, "connect", streaming_connect)

    result = normalize_database(db_path, table="mind_sessions", apply=False)

    assert result["rows"] == 2
    assert result["changed_rows"] == 1


@pytest.mark.parametrize(
    "table",
    ["mind_sessions; DROP TABLE mind_sessions", "mind-sessions", "main.mind_sessions", ""],
)
def test_rejects_unsafe_table_identifiers(sessions_db: tuple[Path, object], table: str) -> None:
    """Dynamic table names are restricted to a single safe identifier."""
    db_path, _ = sessions_db

    with pytest.raises(ValueError, match="safe SQLite identifier"):
        normalize_database(db_path, table=table, apply=False)


def test_rejects_schema_without_exact_required_columns(tmp_path: Path) -> None:
    """The target table must expose the two production columns."""
    db_path = tmp_path / "wrong.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE mind_sessions (session_id TEXT PRIMARY KEY, payload JSON)")

    with pytest.raises(ValueError, match="required columns"):
        normalize_database(db_path, table="mind_sessions", apply=False)


def test_cli_requires_exactly_one_mode(sessions_db: tuple[Path, object]) -> None:
    """CLI callers must explicitly choose inspection or mutation."""
    db_path, _ = sessions_db
    neither = subprocess.run(
        [sys.executable, str(SCRIPT), str(db_path), "--table", "mind_sessions"],
        check=False,
        capture_output=True,
        text=True,
    )
    both = _run_cli(db_path, "--dry-run", "--apply")

    assert neither.returncode == 2
    assert both.returncode == 2
