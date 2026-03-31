"""Tests for the Agno double-encoded JSON monkey-patch and migration script."""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from agno.db.utils import CustomJSONEncoder
from sqlalchemy import Column, MetaData, String, Table, create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.types import JSON

from mindroom.patches.agno_json_fix import _patched_deserialize, _patched_serialize
from scripts.fix_double_encoded_sessions import fix_database

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_runs() -> list[dict[str, Any]]:
    """Return a minimal runs list resembling Agno RunOutput.to_dict() output."""
    return [
        {
            "run_id": "run-001",
            "agent_id": "agent-test",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        },
    ]


def _sample_summary() -> dict[str, Any]:
    """Return a minimal session summary dict."""
    return {"summary": "User greeted the agent.", "num_runs": 1}


def _sample_session(
    *,
    runs: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a session dict similar to ``AgentSession.to_dict()``."""
    return {
        "session_id": "sess-001",
        "agent_id": "agent-test",
        "user_id": "user-test",
        "session_data": {"session_name": "test"},
        "agent_data": {"name": "TestAgent", "model": "test-model"},
        "team_data": None,
        "workflow_data": None,
        "metadata": {"source": "test"},
        "chat_history": None,
        "runs": runs if runs is not None else _sample_runs(),
        "summary": summary if summary is not None else _sample_summary(),
    }


# ---------------------------------------------------------------------------
# serialize patch
# ---------------------------------------------------------------------------


class TestPatchedSerialize:
    """The patched serialize must NOT json.dumps JSON fields."""

    def test_runs_stays_native(self) -> None:
        """Runs field must remain a native list after serialization."""
        session = _sample_session()
        result = _patched_serialize(session)
        assert isinstance(result["runs"], list), "runs must remain a native list"
        assert isinstance(result["runs"][0], dict)

    def test_summary_stays_native(self) -> None:
        """Summary field must remain a native dict after serialization."""
        session = _sample_session()
        result = _patched_serialize(session)
        assert isinstance(result["summary"], dict), "summary must remain a native dict"

    def test_session_data_stays_native(self) -> None:
        """Non-CustomJSONEncoder fields pass through as native objects."""
        session = _sample_session()
        result = _patched_serialize(session)
        assert isinstance(result["session_data"], dict)

    def test_none_fields_unchanged(self) -> None:
        """None values should not be touched."""
        session = _sample_session()
        session["team_data"] = None
        result = _patched_serialize(session)
        assert result["team_data"] is None

    def test_uuid_normalized(self) -> None:
        """UUIDs in runs/summary should be converted to strings."""
        uid = uuid4()
        runs: list[dict[str, Any]] = [{"run_id": str(uid), "extra_uuid": uid}]
        session = _sample_session(runs=runs)
        result = _patched_serialize(session)
        assert result["runs"][0]["extra_uuid"] == str(uid)

    def test_datetime_normalized(self) -> None:
        """Datetimes in runs/summary should be converted to ISO strings."""
        dt = datetime(2025, 1, 15, 12, 0, 0, tzinfo=UTC)
        runs: list[dict[str, Any]] = [{"run_id": "r1", "created_at": dt}]
        session = _sample_session(runs=runs)
        result = _patched_serialize(session)
        assert result["runs"][0]["created_at"] == dt.isoformat()

    def test_original_upstream_produces_strings(self) -> None:
        """Verify the upstream function DOES produce strings (the bug)."""
        import agno.db.utils as upstream_mod  # noqa: PLC0415

        # Re-execute the original module source to get unpatched functions.
        assert upstream_mod.__file__ is not None
        source = Path(upstream_mod.__file__).read_text()
        temp_mod = types.ModuleType("_upstream_original")
        temp_mod.__dict__["json"] = json
        temp_mod.__dict__["CustomJSONEncoder"] = CustomJSONEncoder
        exec(compile(source, upstream_mod.__file__, "exec"), temp_mod.__dict__)  # noqa: S102

        original_serialize = temp_mod.serialize_session_json_fields
        session = _sample_session()
        result = original_serialize(session)
        assert isinstance(result["runs"], str), "upstream serialize produces string (the bug)"
        assert isinstance(result["summary"], str)


# ---------------------------------------------------------------------------
# deserialize patch
# ---------------------------------------------------------------------------


class TestPatchedDeserialize:
    """The patched deserialize must handle single-encoded, double-encoded, and native data."""

    def test_native_objects_passthrough(self) -> None:
        """Already-native objects (post-fix) should pass through unchanged."""
        session = _sample_session()
        result = _patched_deserialize(session)
        assert result["runs"] == _sample_runs()
        assert result["summary"] == _sample_summary()

    def test_single_encoded_string(self) -> None:
        """A single JSON string (e.g. from raw SQL read) should be decoded."""
        session = _sample_session()
        session["runs"] = json.dumps(_sample_runs())
        result = _patched_deserialize(session)
        assert isinstance(result["runs"], list)
        assert result["runs"] == _sample_runs()

    def test_double_encoded_string(self) -> None:
        """Legacy double-encoded data should be fully decoded."""
        session = _sample_session()
        session["runs"] = json.dumps(json.dumps(_sample_runs()))
        result = _patched_deserialize(session)
        assert isinstance(result["runs"], list)
        assert result["runs"] == _sample_runs()

    def test_none_unchanged(self) -> None:
        """None values pass through without error."""
        session = _sample_session()
        session["runs"] = None
        result = _patched_deserialize(session)
        assert result["runs"] is None

    def test_invalid_json_string_kept(self) -> None:
        """A non-JSON string should be kept as-is."""
        session = _sample_session()
        session["metadata"] = "not-valid-json{{"
        result = _patched_deserialize(session)
        assert result["metadata"] == "not-valid-json{{"


# ---------------------------------------------------------------------------
# Roundtrip: serialize -> SQLAlchemy JSON column -> deserialize
# ---------------------------------------------------------------------------


@pytest.fixture
def sa_db(tmp_path: Path) -> tuple[Engine, Table]:
    """Create a test SQLite DB with JSON columns."""
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    meta = MetaData()
    table = Table(
        "sessions",
        meta,
        Column("session_id", String, primary_key=True),
        Column("runs", JSON, nullable=True),
        Column("summary", JSON, nullable=True),
        Column("session_data", JSON, nullable=True),
        Column("agent_data", JSON, nullable=True),
        Column("metadata_col", JSON, nullable=True),
    )
    meta.create_all(engine)
    return engine, table


class TestRoundtrip:
    """Simulate the full write-read cycle through a real SQLite JSON column."""

    def test_patched_roundtrip_preserves_data(self, sa_db: tuple[Engine, Table]) -> None:
        """Write with patched serialize, read back, data matches."""
        engine, table = sa_db
        session = _sample_session()
        serialized = _patched_serialize(session.copy())

        with SASession(engine) as sa:
            with sa.begin():
                sa.execute(
                    table.insert().values(
                        session_id=serialized["session_id"],
                        runs=serialized["runs"],
                        summary=serialized["summary"],
                        session_data=serialized["session_data"],
                        agent_data=serialized["agent_data"],
                        metadata_col=serialized["metadata"],
                    ),
                )

            row = sa.execute(table.select()).fetchone()
            assert row is not None
            row_dict: dict[str, Any] = {
                "runs": row.runs,
                "summary": row.summary,
                "session_data": row.session_data,
                "agent_data": row.agent_data,
                "metadata": row.metadata_col,
            }

        result = _patched_deserialize(row_dict)
        assert result["runs"] == _sample_runs()
        assert result["summary"] == _sample_summary()
        assert result["session_data"] == {"session_name": "test"}

    def test_legacy_double_encoded_roundtrip(self, sa_db: tuple[Engine, Table]) -> None:
        """Legacy double-encoded data is read correctly by the patched deserialize."""
        engine, table = sa_db
        runs = _sample_runs()
        # Simulate the old (broken) serialize: json.dumps before SQLAlchemy JSON column.
        double_encoded_runs = json.dumps(runs, cls=CustomJSONEncoder)

        with SASession(engine) as sa:
            with sa.begin():
                sa.execute(
                    table.insert().values(
                        session_id="legacy-001",
                        runs=double_encoded_runs,  # string -> JSON col -> double-encoded
                        summary=None,
                        session_data=None,
                        agent_data=None,
                        metadata_col=None,
                    ),
                )

            row = sa.execute(table.select()).fetchone()
            assert row is not None
            row_dict: dict[str, Any] = {"runs": row.runs, "summary": row.summary}

        result = _patched_deserialize(row_dict)
        assert isinstance(result["runs"], list)
        assert result["runs"] == runs


# ---------------------------------------------------------------------------
# Migration script
# ---------------------------------------------------------------------------


@pytest.fixture
def double_encoded_db(tmp_path: Path) -> Path:
    """Create a DB with double-encoded session data."""
    db_path = tmp_path / "test_sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE agno_sessions ("
        "  session_id TEXT PRIMARY KEY,"
        "  runs TEXT,"
        "  summary TEXT,"
        "  session_data TEXT,"
        "  agent_data TEXT,"
        "  metadata TEXT"
        ")",
    )

    runs = _sample_runs()
    summary = _sample_summary()
    agent_data = {"name": "TestAgent"}

    # Double-encode: json.dumps twice (what SQLAlchemy JSON column does
    # on top of the upstream serialize).
    conn.execute(
        "INSERT INTO agno_sessions VALUES (?, ?, ?, ?, ?, ?)",
        (
            "sess-double",
            json.dumps(json.dumps(runs)),
            json.dumps(json.dumps(summary)),
            json.dumps(json.dumps({"session_name": "test"})),
            json.dumps(json.dumps(agent_data)),
            json.dumps(json.dumps({"source": "test"})),
        ),
    )
    # Also insert a correctly-encoded row (should not be modified).
    conn.execute(
        "INSERT INTO agno_sessions VALUES (?, ?, ?, ?, ?, ?)",
        (
            "sess-correct",
            json.dumps(runs),
            json.dumps(summary),
            json.dumps({"session_name": "ok"}),
            json.dumps(agent_data),
            json.dumps({"source": "ok"}),
        ),
    )
    conn.commit()
    conn.close()
    return db_path


class TestMigrationScript:
    """Test the migration script against a synthetic double-encoded DB."""

    def test_fixes_double_encoded_rows(self, double_encoded_db: Path) -> None:
        """Only the double-encoded row should be fixed."""
        checked, fixed, saved = fix_database(double_encoded_db)
        assert checked == 2
        assert fixed == 1
        assert saved > 0

        conn = sqlite3.connect(str(double_encoded_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM agno_sessions WHERE session_id = 'sess-double'",
        ).fetchone()
        conn.close()

        runs = json.loads(row["runs"])
        assert isinstance(runs, list)
        assert runs == _sample_runs()

    def test_dry_run_does_not_modify(self, double_encoded_db: Path) -> None:
        """Dry run reports fixes but does not modify the database."""
        _checked, fixed, saved = fix_database(double_encoded_db, dry_run=True)
        assert fixed == 1
        assert saved > 0

        conn = sqlite3.connect(str(double_encoded_db))
        row = conn.execute(
            "SELECT runs FROM agno_sessions WHERE session_id = 'sess-double'",
        ).fetchone()
        conn.close()

        outer = json.loads(row[0])
        assert isinstance(outer, str), "dry run should not modify data"

    def test_nonexistent_db(self, tmp_path: Path) -> None:
        """A missing DB file should return zero counts."""
        checked, fixed, _saved = fix_database(tmp_path / "nope.db")
        assert checked == 0
        assert fixed == 0

    def test_data_correctly_fixed(self, double_encoded_db: Path) -> None:
        """Verify the actual stored JSON is now single-encoded."""
        fix_database(double_encoded_db)
        conn = sqlite3.connect(str(double_encoded_db))
        row = conn.execute(
            "SELECT runs FROM agno_sessions WHERE session_id = 'sess-double'",
        ).fetchone()
        conn.close()
        runs = json.loads(row[0])
        assert isinstance(runs, list)


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------


class TestPatchApplication:
    """Verify the monkey-patch replaces the correct module-level names."""

    def test_agno_db_utils_patched(self) -> None:
        """The agno.db.utils module should have patched functions."""
        import agno.db.utils as mod  # noqa: PLC0415

        assert mod.serialize_session_json_fields is _patched_serialize
        assert mod.deserialize_session_json_fields is _patched_deserialize

    def test_sqlite_module_patched(self) -> None:
        """The agno.db.sqlite.sqlite module should have patched functions."""
        import agno.db.sqlite.sqlite as mod  # noqa: PLC0415

        assert mod.serialize_session_json_fields is _patched_serialize
        assert mod.deserialize_session_json_fields is _patched_deserialize
