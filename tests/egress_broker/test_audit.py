"""Tests for request audit log."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures
from typing import TYPE_CHECKING

import pytest

from mindroom.egress_broker.audit import AuditLog, AuditRecord

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def sample_record() -> AuditRecord:
    """Return a sample audit record for testing."""
    return AuditRecord(
        at=datetime.now(UTC),
        kind="request",
        scope="user_agent",
        agent_name="agent_1",
        requester_id="@user:example.com",
        method="GET",
        host="api.example.com",
        path="/v1/data",
        service="example_api",
        status=200,
        bytes_up=123,
        bytes_down=456,
        duration_ms=789,
    )


@pytest.fixture
def log(tmp_path: Path) -> Iterator[AuditLog]:
    """Return an audit log in ``tmp_path / "audit.db"``, closed after the test."""
    audit_log = AuditLog(tmp_path / "audit.db")
    yield audit_log
    audit_log.close()


def _record_series(
    log: AuditLog,
    template: AuditRecord,
    count: int,
    *,
    agent_name: str,
    path_prefix: str,
    age: timedelta,
) -> None:
    """Record `count` entries for `agent_name`, each `age` plus its index seconds old, with the index as every counter."""
    now = datetime.now(UTC)
    for i in range(count):
        log.record(
            replace(
                template,
                at=now - age - timedelta(seconds=i),
                agent_name=agent_name,
                path=f"{path_prefix}_{i}",
                service=None,
                bytes_up=i,
                bytes_down=i,
                duration_ms=i,
            ),
        )


def test_record_and_query_newest_first(log: AuditLog, sample_record: AuditRecord) -> None:
    """Records are stored and returned newest first."""
    # Record three entries with different timestamps
    now = datetime.now(UTC)
    rec1 = replace(sample_record, at=now - timedelta(seconds=2))
    rec2 = replace(
        sample_record,
        at=now - timedelta(seconds=1),
        kind="tunnel",
        scope="user_agent",
        agent_name="agent_2",
        method="CONNECT",
        host="db.example.com",
        path="",
        service=None,
        bytes_up=1000,
        bytes_down=2000,
        duration_ms=5000,
    )
    rec3 = replace(
        sample_record,
        at=now,
        kind="denied",
        scope="shared",
        agent_name=None,
        requester_id=None,
        method="POST",
        host="blocked.example.com",
        path="/admin",
        service=None,
        status=403,
        bytes_up=50,
        bytes_down=0,
        duration_ms=10,
    )

    log.record(rec1)
    log.record(rec2)
    log.record(rec3)

    # Query all records
    results = log.query()
    assert len(results) == 3
    # Newest first
    assert results[0].at == rec3.at
    assert results[1].at == rec2.at
    assert results[2].at == rec1.at

    # Verify all fields round-trip correctly
    assert results[0].kind == "denied"
    assert results[0].scope == "shared"
    assert results[0].agent_name is None
    assert results[0].requester_id is None
    assert results[0].method == "POST"
    assert results[0].host == "blocked.example.com"
    assert results[0].path == "/admin"
    assert results[0].service is None
    assert results[0].status == 403
    assert results[0].bytes_up == 50
    assert results[0].bytes_down == 0
    assert results[0].duration_ms == 10


def test_query_string_is_never_stored(tmp_path: Path, log: AuditLog, sample_record: AuditRecord) -> None:
    """Query strings are stripped from paths and not present in the database file."""
    # Record a request with a query string containing a token
    log.record(replace(sample_record, path="/v1/data?token=abc123&key=secret"))

    # Verify the stored path is only the path component
    results = log.query()
    assert len(results) == 1
    assert results[0].path == "/v1/data"

    log.close()

    # Verify that the query string is NOT present in the raw database file bytes
    db_bytes = (tmp_path / "audit.db").read_bytes()
    assert b"abc" not in db_bytes
    assert b"token" not in db_bytes
    assert b"secret" not in db_bytes
    assert b"/v1/data" in db_bytes


def test_filters_and_limit(log: AuditLog, sample_record: AuditRecord) -> None:
    """Query filters work and limit caps the number of records."""
    # Record several entries with different attributes
    now = datetime.now(UTC)
    for i in range(10):
        log.record(
            replace(
                sample_record,
                at=now + timedelta(seconds=i),
                agent_name=f"agent_{i % 3}",
                host=f"host_{i % 2}.example.com",
                path=f"/path_{i}",
                service=f"service_{i % 4}" if i % 2 == 0 else None,
                bytes_up=i,
                bytes_down=i * 2,
                duration_ms=i * 10,
            ),
        )

    # Filter by agent_name
    results = log.query(agent_name="agent_1")
    assert len(results) > 0
    assert all(r.agent_name == "agent_1" for r in results)

    # Filter by host
    results = log.query(host="host_0.example.com")
    assert len(results) > 0
    assert all(r.host == "host_0.example.com" for r in results)

    # Filter by service
    results = log.query(service="service_0")
    assert len(results) > 0
    assert all(r.service == "service_0" for r in results)

    # Multiple filters
    results = log.query(agent_name="agent_0", host="host_0.example.com")
    assert len(results) > 0
    assert all(r.agent_name == "agent_0" and r.host == "host_0.example.com" for r in results)

    # Normal limit
    results = log.query(limit=3)
    assert len(results) == 3


def test_requester_filter_matches_only_that_requester(log: AuditLog, sample_record: AuditRecord) -> None:
    """The requester filter is an exact match, combines with the other filters, and skips rows without a requester."""
    now = datetime.now(UTC)
    for i, requester in enumerate(["@alice:example.org", "@bob:example.org", None, "@alice:example.org.evil"]):
        for agent in ("agent_1", "agent_2"):
            log.record(
                replace(
                    sample_record,
                    at=now + timedelta(seconds=i),
                    scope="shared",
                    agent_name=agent,
                    requester_id=requester,
                    path=f"/{requester}/{agent}",
                    service=None,
                    bytes_up=0,
                    bytes_down=0,
                    duration_ms=1,
                ),
            )

    alice = log.query(requester_id="@alice:example.org")
    assert {(r.requester_id, r.agent_name) for r in alice} == {
        ("@alice:example.org", "agent_1"),
        ("@alice:example.org", "agent_2"),
    }
    assert log.query(requester_id="@alice:example.org", agent_name="agent_2")[0].path == "/@alice:example.org/agent_2"
    assert len(log.query(requester_id="@alice:example.org", agent_name="agent_2")) == 1
    assert log.query(requester_id="@nobody:example.org") == []
    assert len(log.query()) == 8


def test_the_thousandth_record_prunes_rows_older_than_retention(tmp_path: Path, sample_record: AuditRecord) -> None:
    """Rows beyond retention_days stay until the 1000th insert, which prunes them."""
    log = AuditLog(tmp_path / "audit.db", retention_days=7)
    _record_series(log, sample_record, 10, agent_name="agent_old", path_prefix="/old", age=timedelta(days=10))
    _record_series(log, sample_record, 989, agent_name="agent_recent", path_prefix="/recent", age=timedelta(days=3))
    assert len(log.query(limit=2000)) == 999

    _record_series(log, sample_record, 1, agent_name="agent_recent", path_prefix="/last", age=timedelta(days=3))

    results = log.query(limit=2000)
    assert len(results) == 990
    assert all(r.agent_name == "agent_recent" for r in results)

    log.close()


def test_the_thousandth_record_trims_to_max_rows_keeping_the_newest(tmp_path: Path, sample_record: AuditRecord) -> None:
    """Rows beyond max_rows stay until the 1000th insert, which deletes the oldest of them."""
    log = AuditLog(tmp_path / "audit.db", max_rows=100)
    _record_series(log, sample_record, 500, agent_name="agent_older", path_prefix="/older", age=timedelta(days=3))
    _record_series(log, sample_record, 499, agent_name="agent_newer", path_prefix="/newer", age=timedelta(hours=1))
    assert len(log.query(limit=2000)) == 999

    _record_series(log, sample_record, 1, agent_name="agent_newer", path_prefix="/last", age=timedelta(hours=1))

    results = log.query(limit=2000)
    assert len(results) == 100
    assert all(r.agent_name == "agent_newer" for r in results)

    log.close()


def test_reopening_prunes_rows_older_than_retention_without_a_thousand_inserts(
    tmp_path: Path,
    sample_record: AuditRecord,
) -> None:
    """A process that restarts before its 1000th insert still drops rows past retention_days."""
    log = AuditLog(tmp_path / "audit.db", retention_days=7)
    _record_series(log, sample_record, 3, agent_name="agent_old", path_prefix="/old", age=timedelta(days=10))
    _record_series(log, sample_record, 5, agent_name="agent_recent", path_prefix="/recent", age=timedelta(days=3))
    assert len(log.query()) == 8
    log.close()

    reopened = AuditLog(tmp_path / "audit.db", retention_days=7)

    results = reopened.query()
    assert len(results) == 5
    assert all(r.agent_name == "agent_recent" for r in results)

    reopened.close()


def test_reopening_trims_to_max_rows_keeping_the_newest(tmp_path: Path, sample_record: AuditRecord) -> None:
    """A process that restarts before its 1000th insert still enforces max_rows."""
    log = AuditLog(tmp_path / "audit.db", max_rows=4)
    _record_series(log, sample_record, 6, agent_name="agent_older", path_prefix="/older", age=timedelta(days=3))
    _record_series(log, sample_record, 4, agent_name="agent_newer", path_prefix="/newer", age=timedelta(hours=1))
    assert len(log.query()) == 10
    log.close()

    reopened = AuditLog(tmp_path / "audit.db", max_rows=4)

    results = reopened.query()
    assert len(results) == 4
    assert all(r.agent_name == "agent_newer" for r in results)

    reopened.close()


def test_concurrent_record_from_threads(log: AuditLog, sample_record: AuditRecord) -> None:
    """Concurrent writes from multiple threads all succeed."""

    def record_many(thread_id: int) -> None:
        for i in range(100):
            log.record(
                replace(
                    sample_record,
                    at=datetime.now(UTC),
                    agent_name=f"agent_{thread_id}",
                    path=f"/thread_{thread_id}/record_{i}",
                    service=None,
                    bytes_up=i,
                    bytes_down=i,
                    duration_ms=i,
                ),
            )

    # Start 8 threads, each recording 100 records
    threads = []
    for tid in range(8):
        t = threading.Thread(target=record_many, args=(tid,))
        threads.append(t)
        t.start()

    # Wait for all threads to complete
    for t in threads:
        t.join()

    # Verify all 800 records were inserted
    results = log.query(limit=1000)
    assert len(results) == 800

    # Verify we have records from all 8 threads
    agent_names = {r.agent_name for r in results}
    assert len(agent_names) == 8
    assert all(f"agent_{i}" in agent_names for i in range(8))


def test_refusal_code_is_stored_and_a_forward_has_none(log: AuditLog, sample_record: AuditRecord) -> None:
    """A refusal keeps the error code its body carried; a forwarded request reads back as None."""
    now = datetime.now(UTC)
    log.record(replace(sample_record, at=now - timedelta(seconds=1), path="/ok"))
    log.record(replace(sample_record, at=now, kind="denied", status=403, path="/other", code="path_not_allowed"))

    refused, forwarded = log.query()

    assert (refused.path, refused.code) == ("/other", "path_not_allowed")
    assert (forwarded.path, forwarded.code) == ("/ok", None)
