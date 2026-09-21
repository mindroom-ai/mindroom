"""Freeze local evidence and join exact notice events without inventing queue state."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO

_MARKER = "mindroom_queued_message_notice"
_OWNER = "mindroom_queued_message_notice_response_turn_id"
_PARTICIPATION = "Decide whether to participate in this conversation now."


def digest(data: bytes) -> str:
    """Return an artifact hash without exposing its contents."""
    return hashlib.sha256(data).hexdigest()


def private_directory(path: Path) -> None:
    """Create a private output directory, refusing symlink ancestors."""
    refuse_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def refuse_symlinks(path: Path) -> None:
    """Refuse a symlink at any existing component, including the root argument."""
    for part in (path, *path.parents):
        if part.is_symlink():
            msg = "symlink"
            raise ValueError(msg)


def write_private(path: Path, value: object, *, lines: bool = False) -> None:
    """Write new private artifacts without following or overwriting existing files."""
    private_directory(path.parent)
    content = (
        "\n".join(json.dumps(row, sort_keys=True) for row in value)
        if lines
        else json.dumps(value, indent=2, sort_keys=True)
    )
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as handle:
        handle.write(content + "\n")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            msg = "duplicate_key"
            raise ValueError(msg)
        result[key] = value
    return result


def _require_object(value: object) -> None:
    if not isinstance(value, dict):
        msg = "non_object"
        raise TypeError(msg)


def read_records(path: Path, *, errors: list[dict] | None = None) -> Iterator[tuple[int, dict]]:
    """Read adjacent complete JSON objects; quarantine the entire tail on corruption.

    Offsets are bytes in the frozen file. Never search inside a corrupt record for
    another brace, and never join records across the resulting integrity gap.
    """
    refuse_symlinks(path)
    with path.open("rb") as handle:
        yield from _read_record_stream(handle, path, errors)


def records_from_bytes(raw: bytes) -> list[dict]:
    """Parse the exact reviewed bytes; do not reopen a mutable outgoing file."""
    errors = []
    rows = [row for _, row in _read_record_stream(io.BytesIO(raw), Path("reviewed-input"), errors)]
    if errors:
        msg = "malformed input artifact"
        raise ValueError(msg)
    return rows


def _read_record_stream(handle: BinaryIO, path: Path, errors: list[dict] | None) -> Iterator[tuple[int, dict]]:
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    offset = 0
    for raw in handle:
        try:
            line = raw.decode("utf-8")
            cursor = 0
            while cursor < len(line):
                if line[cursor].isspace():
                    cursor += 1
                    continue
                start = cursor
                row, cursor = decoder.raw_decode(line, cursor)
                _require_object(row)
                yield offset + len(line[:start].encode()), row
        except (ValueError, TypeError, RecursionError):
            if errors is not None:
                errors.append({"path": str(path), "offset": offset, "reason": "malformed_tail"})
            return
        offset += len(raw)


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _kind(path: Path) -> str | None:
    parts = path.parts
    if len(parts) == 2 and parts[0] == "logs" and path.match("mindroom_*.log"):
        return "logs"
    if parts[:2] == ("logs", "llm_requests") and path.suffix == ".jsonl":
        return "provider_requests"
    if "thread_exports" in parts and path.suffix in {".yaml", ".yml"}:
        return "exports"
    if "sessions" in parts and path.suffix == ".db" and parts[0] in {"agents", "teams"}:
        return "sessions"
    if parts == ("tracking", "event_journal.db"):
        return "journal"
    return None


def _within_scope(relative: Path) -> bool:
    parts = relative.parts
    if parts[0] == "logs":
        return relative.name == "llm_requests"
    if parts[0] == "tracking":
        return False
    if parts[0] in {"agents", "teams"} and len(parts) == 3:
        return relative.name in {"sessions", "workspace"}
    return not (len(parts) == 4 and parts[2] == "workspace" and relative.name != "thread_exports")


def _sources(root: Path, omissions: Counter) -> Iterator[tuple[Path, str]]:
    # Prune unrelated storage (credentials, media, browser, private instances).
    for base in (root / "logs", root / "agents", root / "teams", root / "tracking"):
        if not base.exists():
            continue
        if base.is_symlink():
            omissions["symlink"] += 1
            continue
        for directory, dirs, files in os.walk(base, followlinks=False):
            parent = Path(directory)
            allowed = []
            for name in sorted(dirs):
                child = parent / name
                relative = child.relative_to(root)
                if child.is_symlink():
                    omissions["symlink"] += 1
                elif not _within_scope(relative):
                    continue
                else:
                    allowed.append(name)
            dirs[:] = allowed
            for name in sorted(files):
                path = parent / name
                kind = _source_kind(path, root, omissions)
                if kind:
                    yield path, kind


def _source_kind(path: Path, root: Path, omissions: Counter) -> str | None:
    kind = _kind(path.relative_to(root))
    if not kind:
        return None
    if path.is_symlink():
        omissions["symlink"] += 1
        return None
    if not path.is_file():
        omissions["non_regular_file"] += 1
        return None
    return kind


def _freeze(source: Path, destination: Path, *, database: bool) -> dict:
    refuse_symlinks(source)
    private_directory(destination.parent)
    before = source.stat()
    if not stat.S_ISREG(before.st_mode):
        msg = "non_regular_file"
        raise ValueError(msg)
    # Exclusive creation prevents overwriting evidence. SQLite backup includes WAL
    # content in a consistent transaction, unlike copying the live main DB file.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    if database:
        with (
            closing(sqlite3.connect(f"{source.absolute().as_uri()}?mode=ro", uri=True)) as original,
            closing(sqlite3.connect(destination)) as snapshot,
        ):
            original.backup(snapshot)
    else:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as original, destination.open("wb") as snapshot:
            while chunk := original.read(1024 * 1024):
                snapshot.write(chunk)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            destination.unlink()
            msg = "changed_during_copy"
            raise ValueError(msg)
    return {
        "sha256": _file_hash(destination),
        "bytes": destination.stat().st_size,
        "source_mtime_ns": before.st_mtime_ns,
        "snapshot_method": "sqlite_backup" if database else "stable_copy",
    }


def verify_manifest(evidence: Path) -> dict:
    """Verify every frozen source before annotation, evaluation or reporting."""
    refuse_symlinks(evidence / "manifest.json")
    manifest = json.loads((evidence / "manifest.json").read_text())
    for entry in [*manifest["sources"], *manifest.get("artifacts", [])]:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            msg = "manifest integrity: unsafe path"
            raise ValueError(msg)
        path = evidence / relative
        refuse_symlinks(path)
        if _file_hash(path) != entry["sha256"]:
            msg = "manifest integrity mismatch"
            raise ValueError(msg)
    return {"files": len(manifest["sources"]), "manifest_sha256": _file_hash(evidence / "manifest.json")}


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        msg = "invalid_timestamp"
        raise TypeError(msg)
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        msg = "invalid_timestamp"
        raise ValueError(msg)
    return result.astimezone(UTC)


def _markers(messages: object) -> Iterator[tuple[int, str, bool]]:
    if not isinstance(messages, list):
        return
    for position, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        metadata = message.get("provider_data")
        if not isinstance(metadata, dict):
            continue
        owner = metadata.get(_OWNER)
        marker = metadata.get(_MARKER)
        if isinstance(owner, str) and (marker is True or marker == "persisted"):
            yield position, owner, marker == "persisted"


def _seed_cases(entries: list[dict], evidence: Path, errors: list[dict]) -> tuple[dict[str, dict], int]:
    cases = {}
    events = 0
    for entry in entries:
        if entry["kind"] != "logs":
            continue
        for offset, row in read_records(evidence / entry["path"], errors=errors):
            if row.get("event") != "queued_message_notice_injected":
                continue
            events += 1
            owner = row.get("response_turn_id")
            if not isinstance(owner, str) or not owner:
                errors.append({"reason": "notice_missing_owner", "path": entry["path"], "offset": offset})
                continue
            try:
                timestamp = _timestamp(row.get("timestamp")).isoformat()
            except (ValueError, TypeError):
                errors.append({"reason": "notice_invalid_timestamp", "path": entry["path"], "offset": offset})
                continue
            if owner not in cases:
                cases[owner] = {
                    "case_id": digest(owner.encode()),
                    "surface": "B",
                    "response_turn_id": owner,
                    "timestamp": timestamp,
                    "room_id": row.get("room_id"),
                    "thread_id": row.get("thread_id"),
                    "reply_to_event_id": row.get("reply_to_event_id"),
                    "agent_id": row.get("agent_id"),
                    "session_id": row.get("session_id"),
                    "thread_key": digest(json.dumps([row.get("room_id"), row.get("thread_id")]).encode()),
                    "sources": {"logs": [], "provider_requests": [], "exports": [], "sessions": [], "journal": []},
                    "pending_membership_known": False,
                    "tier": "unobservable",
                    "exclusions": ["pending_membership_unobservable"],
                    "boundary_count": None,
                }
            cases[owner]["sources"]["logs"].append({"path": entry["path"], "offset": offset})
    return cases, events


def _same_owner_context(case: dict, record: dict) -> bool:
    # Exact current trigger and scope prevent persisted prior-turn notice reuse.
    return (
        bool(case["reply_to_event_id"])
        and all(record.get(key) == case[key] for key in ("room_id", "thread_id", "reply_to_event_id"))
        and (not case["agent_id"] or record.get("agent_id") == case["agent_id"])
    )


def _participation_candidate(messages: object) -> bool:
    return isinstance(messages, list) and any(
        isinstance(message, dict)
        and isinstance(message.get("content"), str)
        and message["content"].startswith(_PARTICIPATION)
        for message in messages
    )


def _request_after_notice(row: dict, case: dict, stats: Counter) -> bool:
    try:
        valid = _timestamp(row.get("timestamp")) >= _timestamp(case["timestamp"])
    except (ValueError, TypeError):
        stats["undated_request_excluded"] += 1
        return False
    if not valid:
        stats["pre_notice_request_excluded"] += 1
    return valid


def _join_requests(entries: list[dict], evidence: Path, cases: dict, errors: list[dict]) -> dict:
    stats = Counter()
    for entry in entries:
        if entry["kind"] != "provider_requests":
            continue
        for offset, row in read_records(evidence / entry["path"], errors=errors):
            stats["records"] += 1
            if row.get("record") == "response":
                stats["response_records"] += 1
                continue
            messages = row.get("messages", [])
            if _participation_candidate(messages):
                stats["participation_candidates"] += 1
            for position, owner, persisted in _markers(messages):
                case = cases.get(owner)
                if case is None or not _same_owner_context(case, row):
                    stats["historical_or_unjoined_markers"] += 1
                    continue
                if persisted:
                    stats["persisted_markers_excluded"] += 1
                    continue
                if not _request_after_notice(row, case, stats):
                    continue
                case["sources"]["provider_requests"].append(
                    {
                        "path": entry["path"],
                        "offset": offset,
                        "notice_position": position,
                        "request_log_id": row.get("request_log_id"),
                        "projected_redacted_record": True,
                    },
                )
    return dict(stats)


def _deduplicate_exports(messages: list[dict], key: tuple, matching: list[dict], seen: dict, stats: Counter) -> None:
    for message in messages:
        identity = (*key, message.get("event_id"), message.get("latest_event_id"))
        content_hash = digest(json.dumps(message, sort_keys=True).encode())
        if identity in seen:
            stats["duplicate_messages" if seen[identity] == content_hash else "conflicting_messages"] += 1
            if seen[identity] != content_hash:
                for case in matching:
                    if "conflicting_exports" not in case["exclusions"]:
                        case["exclusions"].append("conflicting_exports")
        else:
            seen[identity] = content_hash


def _join_exports(entries: list[dict], evidence: Path, cases: dict, errors: list[dict]) -> dict:
    by_thread = defaultdict(list)
    for case in cases.values():
        by_thread[(case["room_id"], case["thread_id"])].append(case)
    seen = {}
    stats = Counter()
    for entry in entries:
        if entry["kind"] != "exports":
            continue
        try:
            row = yaml.safe_load((evidence / entry["path"]).read_text())
        except (yaml.YAMLError, UnicodeError):
            errors.append({"path": entry["path"], "reason": "malformed_export"})
            continue
        if not isinstance(row, dict) or "messages" not in row:
            stats["index_or_non_thread_files"] += 1
            continue
        if (
            row.get("version") != 1
            or not isinstance(row.get("room"), dict)
            or not isinstance(row.get("thread"), dict)
            or not isinstance(row["messages"], list)
        ):
            errors.append({"path": entry["path"], "reason": "unsupported_export_schema"})
            continue
        key = (row["room"].get("id"), row["thread"].get("id"))
        matching = by_thread.get(key, [])
        messages = [m for m in row["messages"] if isinstance(m, dict)]
        stats["thread_files"] += 1
        _deduplicate_exports(messages, key, matching, seen, stats)
        for case in matching:
            if not case["reply_to_event_id"] or not any(
                m.get("event_id") == case["reply_to_event_id"] for m in messages
            ):
                stats["thread_matches_without_trigger"] += 1
                continue
            case["sources"]["exports"].append(
                {
                    "path": entry["path"],
                    "trigger_present": any(m.get("event_id") == case["reply_to_event_id"] for m in messages),
                    "outcomes_only": True,
                },
            )
    stats["unique_message_versions"] = len(seen)
    return dict(stats)


def _join_journal(db: sqlite3.Connection, cases: dict, entry: dict) -> None:
    for case in cases.values():
        if not case["reply_to_event_id"] or not case["agent_id"]:
            continue
        rows = db.execute(
            "SELECT a.principal_id, a.selected_receipt_order, j.thread_id "
            "FROM response_attempts a JOIN journal_events j ON j.principal_id=a.principal_id "
            "AND j.event_id=a.driving_event_id WHERE a.room_id=? AND a.driving_event_id=? "
            "AND a.entity_name=?",
            (case["room_id"], case["reply_to_event_id"], case["agent_id"]),
        )
        for principal, receipt, thread in rows:
            if thread == case["thread_id"]:
                case["sources"]["journal"].append(
                    {
                        "path": entry["path"],
                        "principal_id": principal,
                        "selected_receipt_order": receipt,
                        "proves_pending_membership": False,
                    },
                )


def _join_sessions(db: sqlite3.Connection, tables: set[str], cases: dict, entry: dict, errors: list[dict]) -> None:
    for table in sorted(tables):
        if not table.endswith("_runs") or not re.fullmatch(r"[A-Za-z0-9_]+", table):
            continue
        columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
        if not {"run_id", "session_id", "run_data"} <= columns:
            continue
        query = f'SELECT run_id, session_id, run_data FROM "{table}" WHERE run_data LIKE ?'  # noqa: S608 — table is identifier-validated above
        rows = db.execute(query, (f"%{_OWNER}%",))
        for run_id, session_id, raw in rows:
            try:
                run = json.loads(raw)
            except (ValueError, TypeError):
                errors.append({"path": entry["path"], "reason": "malformed_session_run"})
                continue
            if not isinstance(run, dict):
                continue
            for position, owner, persisted in _markers(run.get("messages")):
                case = cases.get(owner)
                if case is not None and case["session_id"] == session_id:
                    case["sources"]["sessions"].append(
                        {
                            "path": entry["path"],
                            "run_id": run_id,
                            "notice_position": position,
                            "persisted": persisted,
                            "position_evidence_only": True,
                        },
                    )


def _join_databases(entries: list[dict], evidence: Path, cases: dict, errors: list[dict]) -> None:
    for entry in entries:
        if entry["kind"] not in {"sessions", "journal"}:
            continue
        path = evidence / entry["path"]
        with closing(sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro&immutable=1", uri=True)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if entry["kind"] == "journal":
                if not {"response_attempts", "journal_events"} <= tables:
                    errors.append({"path": entry["path"], "reason": "unsupported_journal_schema"})
                    continue
                _join_journal(db, cases, entry)
                continue
            _join_sessions(db, tables, cases, entry, errors)


def _split_cases(cases: list[dict], validation_after: str | None, holdout_after: str | None) -> dict:
    times = sorted(_timestamp(case["timestamp"]) for case in cases)
    validation = _timestamp(validation_after) if validation_after else (times[int(len(times) * 0.6)] if times else None)
    holdout = _timestamp(holdout_after) if holdout_after else (times[int(len(times) * 0.8)] if times else None)
    if validation and holdout and validation >= holdout:
        # Tiny corpora cannot support three non-overlapping chronological splits.
        if validation_after or holdout_after:
            msg = "split cutoffs must increase"
            raise ValueError(msg)
        validation = holdout = None
    threads = defaultdict(list)
    for case in cases:
        timestamp = _timestamp(case["timestamp"])
        case["split"] = (
            "development"
            if validation is None or timestamp < validation
            else ("validation" if timestamp < holdout else "holdout")
        )
        threads[case["thread_key"]].append(case)
    for group in threads.values():
        if len({case["split"] for case in group}) > 1:
            for case in group:
                case["split"] = "excluded"
                case["exclusions"].append("thread_crosses_cutoff")
    return {
        "validation_after": validation.isoformat() if validation else None,
        "holdout_after": holdout.isoformat() if holdout else None,
        "counts": dict(Counter(case["split"] for case in cases)),
    }


def _relative_exclusions(errors: list[dict], evidence: Path) -> None:
    for error in errors:
        if "path" in error:
            path = Path(error["path"])
            if path.is_relative_to(evidence):
                error["path"] = str(path.relative_to(evidence))


def extract_corpus(
    source: Path,
    evidence: Path,
    *,
    validation_after: str | None = None,
    holdout_after: str | None = None,
    source_revision: str = "unspecified",
) -> dict:
    """Freeze selected local sources, inventory exact events and report join coverage."""
    refuse_symlinks(source)
    refuse_symlinks(evidence)
    if evidence.exists():
        msg = "evidence directory already exists; choose a fresh snapshot"
        raise ValueError(msg)
    source = source.resolve()
    evidence = evidence.resolve()
    if source == evidence or source in evidence.parents:
        msg = "evidence must be outside the live corpus"
        raise ValueError(msg)
    private_directory(evidence)
    entries = []
    omissions = Counter()
    errors = []
    for path, kind in _sources(source, omissions):
        relative = Path("corpus") / path.relative_to(source)
        try:
            metadata = _freeze(path, evidence / relative, database=kind in {"sessions", "journal"})
        except (ValueError, OSError, sqlite3.Error) as error:
            (evidence / relative).unlink(missing_ok=True)
            reason = str(error) if isinstance(error, ValueError) else "snapshot_failed"
            omissions[reason] += 1
            errors.append({"path": str(relative), "reason": reason})
            continue
        entries.append({"path": str(relative), "kind": kind, **metadata})
    cases, events = _seed_cases(entries, evidence, errors)
    request_stats = _join_requests(entries, evidence, cases, errors)
    export_stats = _join_exports(entries, evidence, cases, errors)
    _join_databases(entries, evidence, cases, errors)
    ordered = sorted(cases.values(), key=lambda row: (row["timestamp"], row["case_id"]))
    for case in ordered:
        if any(case["sources"][kind] for kind in ("provider_requests", "exports", "sessions", "journal")):
            case["tier"] = "partial"
        if not case["reply_to_event_id"] or not case["session_id"]:
            case["exclusions"].append("missing_reply_or_session_correlation")
        observations = case["sources"]["provider_requests"]
        case["observed_provider_boundaries"] = len(
            {row["request_log_id"] or (row["path"], row["offset"]) for row in observations},
        )
    _relative_exclusions(errors, evidence)
    splits = _split_cases(ordered, validation_after, holdout_after)
    report = {
        "notice_events": events,
        "notice_turns": len(cases),
        "agent_counts": dict(Counter(case["agent_id"] or "unattributed" for case in ordered)),
        "source_files": dict(Counter(entry["kind"] for entry in entries)),
        "coverage": {
            kind: sum(bool(case["sources"][kind]) for case in ordered)
            for kind in ("logs", "provider_requests", "exports", "sessions", "journal")
        },
        "provider_boundary_observations": sum(len(case["sources"]["provider_requests"]) for case in ordered),
        "eligible_boundary_count": None,
        "verified_pending_sets": 0,
        "tiers": dict(Counter(case["tier"] for case in ordered)),
        "splits": splits,
        "surface_a": {
            "observed_candidates": request_stats.get("participation_candidates", 0),
            "verified_cohort": 0,
            "status": "no observed A evaluation cohort; eligibility unverified",
        },
        "provider_inventory": request_stats,
        "export_inventory": export_stats,
        "omissions": dict(omissions),
        "integrity_exclusions": len(errors),
        "network_requests": 0,
        "gate": "no_go_missing_pending_membership_labels_and_egress_authorization",
    }
    write_private(evidence / "cases.jsonl", ordered, lines=True)
    write_private(evidence / "coverage.json", report)
    write_private(evidence / "exclusions.json", errors)
    write_private(
        evidence / "outgoing-manifest.json",
        {
            "authorized": False,
            "requests": [],
            "reason": "message egress not authorized; no real inputs prepared for sending",
        },
    )
    artifacts = [
        {"path": name, "sha256": _file_hash(evidence / name)}
        for name in ("cases.jsonl", "coverage.json", "exclusions.json", "outgoing-manifest.json")
    ]
    write_private(
        evidence / "manifest.json",
        {
            "schema": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "source_revision": source_revision,
            "source_root": str(source.absolute()),
            "sources": entries,
            "artifacts": artifacts,
            "splits": splits,
            "privacy": "private raw evidence; no text approved for egress; do not commit",
        },
    )
    return report
