"""Safely coalesce OpenRouter reasoning fragments stored in session runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from mindroom.openai_models import _coalesced_openrouter_reasoning_details

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_COLUMNS = frozenset({"session_id", "runs"})
type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class _RowSummary:
    session_id: str
    raw_sha256: str
    current_json_sha256: str
    expected_json_sha256: str
    changed: bool
    runs: int
    reasoning_details_before: int
    reasoning_details_after: int


@dataclass(frozen=True)
class _ScanSummary:
    rows: tuple[_RowSummary, ...]
    changed_rows: int
    runs: int
    reasoning_details_before: int
    reasoning_details_after: int
    reasoning_text_sha256_before: str
    reasoning_text_sha256_after: str


@dataclass(frozen=True)
class _RowAnalysis:
    summary: _RowSummary
    runs_after: list[JsonValue]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> None:
        """Add bytes to the digest."""

    def hexdigest(self, /) -> str:
        """Return the hexadecimal digest."""


class NormalizationError(RuntimeError):
    """Raised when normalization cannot prove that the database is safe."""

    def __init__(
        self,
        message: str,
        *,
        backup_path: Path | None = None,
        restore_command: str | None = None,
    ) -> None:
        super().__init__(message)
        self.backup_path = backup_path
        self.restore_command = restore_command


def _quoted_table(table: str) -> str:
    if not _SAFE_IDENTIFIER.fullmatch(table):
        msg = f"table must be a safe SQLite identifier, got {table!r}"
        raise ValueError(msg)
    return f'"{table}"'


def _validate_schema(connection: sqlite3.Connection, table: str) -> str:
    quoted_table = _quoted_table(table)
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        msg = f"table {table!r} does not exist"
        raise ValueError(msg)
    columns = {str(info[1]) for info in connection.execute(f"PRAGMA table_info({quoted_table})")}
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        msg = f"table {table!r} is missing required columns: {sorted(missing)}"
        raise ValueError(msg)
    return quoted_table


def _decode_runs(raw_runs: object, session_id: object) -> list[JsonValue]:
    if not isinstance(raw_runs, str):
        msg = f"session {session_id!r} has a non-text runs value"
        raise NormalizationError(msg)
    try:
        encoded_runs: JsonValue = json.loads(raw_runs)
    except json.JSONDecodeError as error:
        msg = f"session {session_id!r} does not contain double-encoded runs JSON: {error}"
        raise NormalizationError(msg) from error
    if not isinstance(encoded_runs, str):
        msg = f"session {session_id!r} has a runs outer JSON value that is not a string"
        raise NormalizationError(msg)
    try:
        runs: JsonValue = json.loads(encoded_runs)
    except json.JSONDecodeError as error:
        msg = f"session {session_id!r} has invalid inner runs JSON: {error}"
        raise NormalizationError(msg) from error
    if not isinstance(runs, list):
        msg = f"session {session_id!r} has a decoded runs value that is not a list"
        raise NormalizationError(msg)
    return runs


def _encode_runs(runs: list[JsonValue]) -> str:
    return json.dumps(json.dumps(runs, separators=(",", ":")), separators=(",", ":"))


def _normalize_value(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    normalized: dict[str, JsonValue] = {}
    for key, child in value.items():
        normalized_child = _normalize_value(child)
        if key == "reasoning_details":
            normalized_child = cast("JsonValue", _coalesced_openrouter_reasoning_details(normalized_child))
        normalized[key] = normalized_child
    return normalized


def _update_reasoning_metrics(value: JsonValue, digest: _Digest) -> int:
    count = 0
    if isinstance(value, list):
        for item in value:
            count += _update_reasoning_metrics(item, digest)
    elif isinstance(value, dict):
        for key, child in value.items():
            if key == "reasoning_details" and isinstance(child, list):
                count += len(child)
                for detail in child:
                    if isinstance(detail, dict) and isinstance(detail.get("text"), str):
                        digest.update(detail["text"].encode())
            count += _update_reasoning_metrics(child, digest)
    return count


def _json_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _analyze_row(
    session_id: object,
    raw_runs: object,
    before_digest: _Digest,
    after_digest: _Digest,
) -> _RowAnalysis:
    if not isinstance(session_id, str):
        msg = f"session_id must be text, got {session_id!r}"
        raise NormalizationError(msg)
    runs_before = _decode_runs(raw_runs, session_id)
    runs_after = _normalize_value(runs_before)
    if not isinstance(runs_after, list):
        msg = f"session {session_id!r} normalization changed the runs container type"
        raise NormalizationError(msg)
    assert isinstance(raw_runs, str)
    summary = _RowSummary(
        session_id=session_id,
        raw_sha256=hashlib.sha256(raw_runs.encode()).hexdigest(),
        current_json_sha256=_json_sha256(runs_before),
        expected_json_sha256=_json_sha256(runs_after),
        changed=runs_before != runs_after,
        runs=len(runs_before),
        reasoning_details_before=_update_reasoning_metrics(runs_before, before_digest),
        reasoning_details_after=_update_reasoning_metrics(runs_after, after_digest),
    )
    return _RowAnalysis(summary=summary, runs_after=runs_after)


def _scan_database(connection: sqlite3.Connection, quoted_table: str) -> _ScanSummary:
    cursor = connection.execute(
        f"SELECT session_id, runs FROM {quoted_table} ORDER BY session_id",  # noqa: S608 -- identifier validated
    )
    rows: list[_RowSummary] = []
    seen_session_ids: set[str] = set()
    before_digest = hashlib.sha256()
    after_digest = hashlib.sha256()
    for session_id, raw_runs in cursor:
        analysis = _analyze_row(session_id, raw_runs, before_digest, after_digest)
        if analysis.summary.session_id in seen_session_ids:
            msg = f"duplicate session_id prevents exact row verification: {analysis.summary.session_id!r}"
            raise NormalizationError(msg)
        seen_session_ids.add(analysis.summary.session_id)
        rows.append(analysis.summary)
        del analysis
    summary = _ScanSummary(
        rows=tuple(rows),
        changed_rows=sum(row.changed for row in rows),
        runs=sum(row.runs for row in rows),
        reasoning_details_before=sum(row.reasoning_details_before for row in rows),
        reasoning_details_after=sum(row.reasoning_details_after for row in rows),
        reasoning_text_sha256_before=before_digest.hexdigest(),
        reasoning_text_sha256_after=after_digest.hexdigest(),
    )
    if summary.reasoning_text_sha256_before != summary.reasoning_text_sha256_after:
        msg = "concatenated reasoning text changed during normalization"
        raise NormalizationError(msg)
    return summary


def _raw_fingerprints(connection: sqlite3.Connection, quoted_table: str) -> tuple[tuple[str, str], ...]:
    cursor = connection.execute(
        f"SELECT session_id, runs FROM {quoted_table} ORDER BY session_id",  # noqa: S608 -- identifier validated
    )
    fingerprints: list[tuple[str, str]] = []
    for session_id, raw_runs in cursor:
        if not isinstance(session_id, str) or not isinstance(raw_runs, str):
            msg = f"invalid session row while fingerprinting: session_id={session_id!r}"
            raise NormalizationError(msg)
        fingerprints.append((session_id, hashlib.sha256(raw_runs.encode()).hexdigest()))
        del raw_runs
    return tuple(fingerprints)


def _integrity_check(connection: sqlite3.Connection, label: str) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        msg = f"{label} failed PRAGMA integrity_check: {rows!r}"
        raise NormalizationError(msg)


def _restore_command(backup_path: Path, database_path: Path) -> str:
    code = (
        "import os, shutil, sys, tempfile\n"
        "source, destination = sys.argv[1], sys.argv[2]\n"
        "directory = os.path.dirname(destination) or '.'\n"
        "def fsync_directory():\n"
        "    descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))\n"
        "    try:\n"
        "        os.fsync(descriptor)\n"
        "    finally:\n"
        "        os.close(descriptor)\n"
        "temporary = None\n"
        "quarantined = []\n"
        "main_replaced = False\n"
        "try:\n"
        "    descriptor, temporary = tempfile.mkstemp(prefix='.' + os.path.basename(destination) + '.restore-', dir=directory)\n"
        "    os.close(descriptor)\n"
        "    shutil.copy2(source, temporary)\n"
        "    with open(temporary, 'rb') as staged:\n"
        "        os.fsync(staged.fileno())\n"
        "    for suffix in ('-wal', '-shm'):\n"
        "        sidecar = destination + suffix\n"
        "        quarantine = temporary + '.quarantine-' + suffix[1:]\n"
        "        try:\n"
        "            os.replace(sidecar, quarantine)\n"
        "        except FileNotFoundError:\n"
        "            pass\n"
        "        else:\n"
        "            quarantined.append((sidecar, quarantine))\n"
        "    fsync_directory()\n"
        "    os.replace(temporary, destination)\n"
        "    temporary = None\n"
        "    main_replaced = True\n"
        "    fsync_directory()\n"
        "    for sidecar, quarantine in quarantined:\n"
        "        os.unlink(quarantine)\n"
        "    quarantined.clear()\n"
        "    fsync_directory()\n"
        "except Exception as error:\n"
        "    rollback_errors = []\n"
        "    if not main_replaced:\n"
        "        for sidecar, quarantine in reversed(quarantined):\n"
        "            if os.path.exists(quarantine):\n"
        "                try:\n"
        "                    os.replace(quarantine, sidecar)\n"
        "                except Exception as rollback_error:\n"
        "                    rollback_errors.append((quarantine, rollback_error))\n"
        "        try:\n"
        "            fsync_directory()\n"
        "        except Exception as rollback_error:\n"
        "            rollback_errors.append((directory, rollback_error))\n"
        "    if rollback_errors:\n"
        "        retained = [path for path, rollback_error in rollback_errors if os.path.exists(path)]\n"
        "        raise RuntimeError('restore failed and quarantine rollback was incomplete; retained quarantine paths: ' + repr(retained)) from error\n"
        "    raise\n"
        "finally:\n"
        "    if temporary is not None:\n"
        "        try:\n"
        "            os.unlink(temporary)\n"
        "        except FileNotFoundError:\n"
        "            pass\n"
    )
    return shlex.join([sys.executable, "-c", code, str(backup_path), str(database_path)])


def _backup_database(
    connection: sqlite3.Connection,
    database_path: Path,
    quoted_table: str,
    source_summary: _ScanSummary,
) -> tuple[Path, str]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = database_path.with_name(f"{database_path.name}.backup-{timestamp}")
    with sqlite3.connect(backup_path) as backup:
        connection.backup(backup)
        _integrity_check(backup, "backup")
        backup_fingerprints = _raw_fingerprints(backup, quoted_table)
    source_fingerprints = tuple((row.session_id, row.raw_sha256) for row in source_summary.rows)
    if backup_fingerprints != source_fingerprints:
        msg = "backup content does not exactly match the source pre-image"
        raise NormalizationError(msg, backup_path=backup_path)
    return backup_path, _restore_command(backup_path, database_path)


def _apply_rows(connection: sqlite3.Connection, quoted_table: str, source_summary: _ScanSummary) -> None:
    connection.execute("BEGIN IMMEDIATE")
    source_fingerprints = tuple((row.session_id, row.raw_sha256) for row in source_summary.rows)
    if _raw_fingerprints(connection, quoted_table) != source_fingerprints:
        msg = "database changed between backup and write transaction"
        raise NormalizationError(msg)

    select_sql = f"SELECT runs FROM {quoted_table} WHERE session_id = ?"  # noqa: S608 -- identifier validated
    update_sql = f"UPDATE {quoted_table} SET runs = ? WHERE session_id = ?"  # noqa: S608 -- identifier validated
    for expected in source_summary.rows:
        selected = connection.execute(select_sql, (expected.session_id,)).fetchone()
        if selected is None:
            msg = f"session disappeared during write transaction: {expected.session_id!r}"
            raise NormalizationError(msg)
        before_digest = hashlib.sha256()
        after_digest = hashlib.sha256()
        analysis = _analyze_row(expected.session_id, selected[0], before_digest, after_digest)
        if analysis.summary != expected:
            msg = f"session changed after preflight scan: {expected.session_id!r}"
            raise NormalizationError(msg)
        if expected.changed:
            cursor = connection.execute(
                update_sql,
                (_encode_runs(analysis.runs_after), expected.session_id),
            )
            if cursor.rowcount != 1:
                msg = f"expected one updated row for session {expected.session_id!r}, got {cursor.rowcount}"
                raise NormalizationError(msg)
        del analysis
    connection.commit()


def _verify_post_image(source: _ScanSummary, actual: _ScanSummary) -> None:
    expected_rows = tuple((row.session_id, row.expected_json_sha256) for row in source.rows)
    actual_rows = tuple((row.session_id, row.current_json_sha256) for row in actual.rows)
    if actual_rows != expected_rows:
        msg = "post-commit rows do not match their exact expected post-images"
        raise NormalizationError(msg)
    if actual.changed_rows != 0:
        msg = "post-commit database still contains coalescible reasoning details"
        raise NormalizationError(msg)
    if actual.runs != source.runs:
        msg = "persisted run count does not match the pre-image"
        raise NormalizationError(msg)
    if actual.reasoning_details_before != source.reasoning_details_after:
        msg = "persisted reasoning-details count does not match the expected post-image"
        raise NormalizationError(msg)
    if actual.reasoning_text_sha256_before != source.reasoning_text_sha256_after:
        msg = "persisted reasoning text does not match the pre-image"
        raise NormalizationError(msg)


def _result(
    summary: _ScanSummary,
    *,
    apply: bool,
    backup_path: Path | None,
    restore_command: str | None,
) -> dict[str, object]:
    return {
        "mode": "apply" if apply else "dry-run",
        "rows": len(summary.rows),
        "changed_rows": summary.changed_rows,
        "runs": summary.runs,
        "reasoning_details_before": summary.reasoning_details_before,
        "reasoning_details_after": summary.reasoning_details_after,
        "reasoning_text_sha256_before": summary.reasoning_text_sha256_before,
        "reasoning_text_sha256_after": summary.reasoning_text_sha256_after,
        "integrity_check": "ok",
        "backup_path": str(backup_path) if backup_path is not None else None,
        "restore_command": restore_command,
    }


def normalize_database(
    database_path: Path,
    *,
    table: str,
    apply: bool,
) -> dict[str, object]:
    """Plan or apply recursive reasoning-details normalization to one database."""
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        msg = f"database does not exist or is not a regular file: {database_path}"
        raise ValueError(msg)

    backup_path: Path | None = None
    restore_command: str | None = None
    connection = sqlite3.connect(database_path)
    try:
        quoted_table = _validate_schema(connection, table)
        _integrity_check(connection, "source")
        if not apply:
            summary = _scan_database(connection, quoted_table)
            return _result(
                summary,
                apply=False,
                backup_path=None,
                restore_command=None,
            )

        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            msg = f"WAL checkpoint could not complete: {checkpoint!r}"
            raise NormalizationError(msg)  # noqa: TRY301 -- caught to attach recovery metadata
        _integrity_check(connection, "source after WAL checkpoint")
        source_summary = _scan_database(connection, quoted_table)
        backup_path, restore_command = _backup_database(
            connection,
            database_path,
            quoted_table,
            source_summary,
        )

        _apply_rows(connection, quoted_table, source_summary)

        _integrity_check(connection, "normalized source")
        actual_summary = _scan_database(connection, quoted_table)
        _verify_post_image(source_summary, actual_summary)
        return _result(
            source_summary,
            apply=True,
            backup_path=backup_path,
            restore_command=restore_command,
        )
    except Exception as error:
        if connection.in_transaction:
            connection.rollback()
        if isinstance(error, ValueError):
            raise
        if isinstance(error, NormalizationError):
            if error.backup_path is None:
                error.backup_path = backup_path
            if error.restore_command is None:
                error.restore_command = restore_command
            raise
        raise NormalizationError(
            str(error),
            backup_path=backup_path,
            restore_command=restore_command,
        ) from error
    finally:
        connection.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="SQLite sessions database")
    parser.add_argument("--table", required=True, help="session table containing session_id and runs")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="inspect without changing database bytes")
    mode.add_argument("--apply", action="store_true", help="back up and apply normalization")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the command-line normalizer."""
    args = _parse_args(argv)
    try:
        result = normalize_database(args.database, table=args.table, apply=args.apply)
    except (NormalizationError, ValueError) as error:
        payload: dict[str, object] = {"error": str(error)}
        if isinstance(error, NormalizationError):
            payload["backup_path"] = str(error.backup_path) if error.backup_path is not None else None
            payload["restore_command"] = error.restore_command
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
