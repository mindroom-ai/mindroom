"""Content-free run usage retained when conversation history is compacted."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, cast

from agno.run.agent import RunOutput
from sqlalchemy import JSON, Column, ForeignKey, Integer, MetaData, String, Table, func, inspect, select
from sqlalchemy.dialects.sqlite import insert

from mindroom import constants
from mindroom.legacy_session_storage import decode_persisted_session_json, merge_legacy_run_payloads
from mindroom.usage_tokens import TOKEN_FIELDS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agno.db.sqlite import SqliteDb
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from sqlalchemy.orm import Session
    from sqlalchemy.sql.selectable import ScalarSelect


def archive_table(db: SqliteDb, *, create: bool = False) -> Table | None:
    """Open the optional archive, creating it only at a compaction write boundary."""
    name = f"{db.session_table_name}_usage"
    metadata = MetaData()
    exists = inspect(db.db_engine).has_table(name)
    if not exists and not create:
        return None
    Table(db.session_table_name, metadata, Column("session_id", String, primary_key=True))
    table = Table(
        name,
        metadata,
        Column(
            "session_id",
            String,
            ForeignKey(f"{db.session_table_name}.session_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("run_id", String, primary_key=True),
        Column("run_index", Integer, nullable=False),
        Column("run_data", JSON, nullable=False),
        Column("created_at", Integer),
    )
    if not exists:
        table.create(db.db_engine, checkfirst=True)
    return table


def _live_table(db: SqliteDb) -> Table:
    return Table(
        db.runs_table_name,
        MetaData(),
        Column("run_id", String),
        Column("session_id", String),
        Column("run_index", Integer),
        Column("created_at", Integer),
        Column("run_data", String),
    )


def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("metadata")
    if not isinstance(raw, dict):
        return {}
    selected: dict[str, Any] = {}
    for key in ("requester_id", constants.MATRIX_EVENT_ID_METADATA_KEY):
        value = raw.get(key)
        if isinstance(value, str):
            selected[key] = value
    for key in (
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
        constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
        constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    ):
        value = raw.get(key)
        if isinstance(value, list):
            selected[key] = [event_id for event_id in value if isinstance(event_id, str)]
    # Only the revision event IDs are needed for redaction matching.
    revisions = raw.get(constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY)
    if isinstance(revisions, dict):
        seen = selected.get(constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
        selected[constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = [
            *(seen if isinstance(seen, list) else []),
            *(
                revision[1]
                for revision in revisions.values()
                if isinstance(revision, (list, tuple)) and len(revision) == 2 and isinstance(revision[1], str)
            ),
        ]
    return selected


def _metric_fields(raw: object, names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError
    values = cast("dict[str, Any]", raw)
    selected = {key: values[key] for key in names if key in values}
    if any(not isinstance(value, (int, float, str, type(None))) for value in selected.values()):
        raise TypeError
    return selected


def _payload(raw: dict[str, Any]) -> dict[str, Any]:
    raw_metrics = raw.get("metrics")
    if raw_metrics is None:
        raw_metrics = {}
    if not isinstance(raw_metrics, dict):
        raise TypeError
    metrics = _metric_fields(raw_metrics, TOKEN_FIELDS)
    details = raw_metrics.get("details")
    if details is not None:
        if not isinstance(details, dict):
            raise TypeError
        models = []
        for entries in details.values():
            if not isinstance(entries, list):
                raise TypeError
            models.extend(_metric_fields(entry, (*TOKEN_FIELDS, "id", "provider")) for entry in entries)
        metrics["details"] = {"models": models}
    return {
        **{
            key: raw.get(key) if isinstance(raw.get(key), str) else None
            for key in ("run_id", "team_id", "user_id", "model", "model_provider")
        },
        "metadata": _metadata(raw),
        "metrics": metrics,
    }


def _raw_runs(db: SqliteDb, transaction: Session, session_id: str) -> dict[str, tuple[dict[str, Any], object]]:
    """Read stored facts directly: deserialization can default an absent timestamp to now."""
    payloads: dict[str, tuple[dict[str, Any], object]] = {}
    columns = {column["name"] for column in inspect(db.db_engine).get_columns(db.session_table_name)}
    if "runs" in columns:
        sessions = Table(db.session_table_name, MetaData(), Column("session_id", String), Column("runs", String))
        legacy = transaction.execute(select(sessions.c.runs).where(sessions.c.session_id == session_id)).scalar_one()
        for value in merge_legacy_run_payloads([], legacy):
            if not isinstance(value, dict):
                continue
            raw = cast("dict[str, Any]", value)
            if isinstance(raw.get("run_id"), str):
                payloads[raw["run_id"]] = (raw, raw.get("created_at"))
    if inspect(db.db_engine).has_table(db.runs_table_name):
        live = _live_table(db)
        for row in transaction.execute(select(live).where(live.c.session_id == session_id)):
            raw = decode_persisted_session_json(row.run_data)
            if not isinstance(raw, dict):
                raise TypeError
            payloads[row.run_id] = (cast("dict[str, Any]", raw), row.created_at)
    return payloads


def archive_runs(db: SqliteDb, session: AgentSession | TeamSession, run_ids: Iterable[str]) -> None:
    """Snapshot top-level usage and run deletion identities before compaction.

    Snapshot the full merged sequence so even legacy-only live runs keep their
    original deletion order. Live facts remain authoritative until compacted.
    """
    if not any(run_ids) or not inspect(db.db_engine).has_table(db.session_table_name):
        return
    table = archive_table(db, create=True)
    assert table is not None
    with db.Session() as transaction, transaction.begin():
        raw_runs = _raw_runs(db, transaction, session.session_id)
        positions = dict(
            transaction.execute(
                select(table.c.run_id, table.c.run_index).where(table.c.session_id == session.session_id),
            ).all(),
        )
        next_position = max(positions.values(), default=-1) + 1
        for run in session.runs or []:
            if run.run_id not in raw_runs:
                continue
            raw, created_at = raw_runs[run.run_id]
            if run.run_id not in positions:
                positions[run.run_id] = next_position
                next_position += 1
            if (
                isinstance(created_at, bool)
                or not isinstance(created_at, (int, float))
                or (isinstance(created_at, float) and not math.isfinite(created_at))
            ):
                created_at = None
            statement = insert(table).values(
                session_id=session.session_id,
                run_id=run.run_id,
                run_index=positions[run.run_id],
                created_at=created_at,
                run_data=_payload(raw)
                if run.parent_run_id is None
                else {
                    "run_id": run.run_id,
                    "parent_run_id": run.parent_run_id,
                    "metadata": _metadata(raw),
                },
            )
            transaction.execute(
                statement.on_conflict_do_update(
                    index_elements=["session_id", "run_id"],
                    set_={"run_data": statement.excluded.run_data, "created_at": statement.excluded.created_at},
                ),
            )


def child_run_ids(transaction: Session, table: Table | None, parent_ids: list[str]) -> list[str]:
    """Find archived descendants for the storage owner's subtree deletion."""
    if table is None:
        return []
    return list(
        transaction.execute(
            select(table.c.run_id).where(table.c.run_data["parent_run_id"].as_string().in_(parent_ids)),
        ).scalars(),
    )


def erase_runs(transaction: Session, table: Table | None, run_ids: set[str]) -> None:
    """Erase archive facts in the same transaction as ordinary run deletion."""
    if table is not None:
        transaction.execute(table.delete().where(table.c.run_id.in_(run_ids)))


def next_run_index(db: SqliteDb, run_id: str | None, session_id: str) -> ScalarSelect[Any] | None:
    """Keep archived order for resurrected IDs and append new runs after both stores."""
    table = archive_table(db)
    if table is None:
        return None
    live = _live_table(db)
    archived_index = (
        select(table.c.run_index).where(table.c.session_id == session_id, table.c.run_id == run_id).scalar_subquery()
    )
    archived_max = (
        select(func.coalesce(func.max(table.c.run_index), -1)).where(table.c.session_id == session_id).scalar_subquery()
    )
    live_max = (
        select(func.coalesce(func.max(live.c.run_index), -1)).where(live.c.session_id == session_id).scalar_subquery()
    )
    return select(func.coalesce(archived_index, func.max(archived_max, live_max) + 1)).scalar_subquery()


def runs_for_deletion(db: SqliteDb, session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    """Merge archived event identities with live runs in original insertion order."""
    table = archive_table(db)
    if table is None:
        return list(session.runs or [])
    with db.Session() as transaction:
        archived = transaction.execute(select(table).where(table.c.session_id == session.session_id)).all()
    archived_positions = {row.run_id: row.run_index for row in archived}
    live_ids = {run.run_id for run in session.runs or []}
    ordered: list[tuple[int, RunOutput | TeamRunOutput]] = [
        (
            row.run_index,
            RunOutput(
                run_id=row.run_id,
                metadata=row.run_data["metadata"],
                parent_run_id=row.run_data.get("parent_run_id"),
            ),
        )
        for row in archived
        if row.run_id not in live_ids
    ]
    next_position = max(archived_positions.values(), default=-1) + 1
    for offset, run in enumerate(session.runs or []):
        ordered.append((archived_positions.get(run.run_id, next_position + offset), run))
    return [run for _, run in sorted(ordered, key=lambda item: item[0])]
