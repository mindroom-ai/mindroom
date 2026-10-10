"""Tests for request audit log."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures

import pytest

from mindroom.egress_broker.audit import AuditLog, AuditRecord


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


def test_record_and_query_newest_first(tmp_path: Path) -> None:
    """Records are stored and returned newest first."""
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path)

    # Record three entries with different timestamps
    now = datetime.now(UTC)
    rec1 = AuditRecord(
        at=now - timedelta(seconds=2),
        kind="request",
        scope="user",
        agent_name="agent_1",
        requester_id="@user:example.com",
        method="GET",
        host="api.example.com",
        path="/v1/data",
        service="example_api",
        status=200,
        bytes_up=100,
        bytes_down=200,
        duration_ms=50,
    )
    rec2 = AuditRecord(
        at=now - timedelta(seconds=1),
        kind="tunnel",
        scope="user_agent",
        agent_name="agent_2",
        requester_id="@user:example.com",
        method="CONNECT",
        host="db.example.com",
        path="",
        service=None,
        status=200,
        bytes_up=1000,
        bytes_down=2000,
        duration_ms=5000,
    )
    rec3 = AuditRecord(
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

    log.close()


def test_query_string_is_never_stored(tmp_path: Path) -> None:
    """Query strings are stripped from paths and not present in the database file."""
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path)

    # Record a request with a query string containing a token
    rec = AuditRecord(
        at=datetime.now(UTC),
        kind="request",
        scope="user",
        agent_name="agent_1",
        requester_id="@user:example.com",
        method="GET",
        host="api.example.com",
        path="/v1/data?token=abc123&key=secret",
        service="example_api",
        status=200,
        bytes_up=100,
        bytes_down=200,
        duration_ms=50,
    )
    log.record(rec)

    # Verify the stored path is only the path component
    results = log.query()
    assert len(results) == 1
    assert results[0].path == "/v1/data"

    log.close()

    # Verify that the query string is NOT present in the raw database file bytes
    db_bytes = db_path.read_bytes()
    assert b"abc" not in db_bytes
    assert b"token" not in db_bytes
    assert b"secret" not in db_bytes
    assert b"/v1/data" in db_bytes


def test_filters_and_limit_clamp(tmp_path: Path) -> None:
    """Query filters work and limit is clamped to 1..1000."""
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path)

    # Record several entries with different attributes
    now = datetime.now(UTC)
    for i in range(10):
        log.record(
            AuditRecord(
                at=now + timedelta(seconds=i),
                kind="request",
                scope="user",
                agent_name=f"agent_{i % 3}",
                requester_id="@user:example.com",
                method="GET",
                host=f"host_{i % 2}.example.com",
                path=f"/path_{i}",
                service=f"service_{i % 4}" if i % 2 == 0 else None,
                status=200,
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

    # Test limit clamping
    # Limit of 0 should be clamped to 1
    results = log.query(limit=0)
    assert len(results) == 1

    # Limit of 2000 should be clamped to 1000
    # (we only have 10 records, but verify the SQL query is constructed correctly)
    results = log.query(limit=2000)
    assert len(results) == 10  # all records since we only have 10

    # Normal limit
    results = log.query(limit=3)
    assert len(results) == 3

    log.close()


def test_requester_filter_matches_only_that_requester(tmp_path: Path) -> None:
    """The requester filter is an exact match, combines with the other filters, and skips rows without a requester."""
    log = AuditLog(tmp_path / "audit.db")
    now = datetime.now(UTC)
    for i, requester in enumerate(["@alice:example.org", "@bob:example.org", None, "@alice:example.org.evil"]):
        for agent in ("agent_1", "agent_2"):
            log.record(
                AuditRecord(
                    at=now + timedelta(seconds=i),
                    kind="request",
                    scope="shared",
                    agent_name=agent,
                    requester_id=requester,
                    method="GET",
                    host="api.example.com",
                    path=f"/{requester}/{agent}",
                    service=None,
                    status=200,
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
    log.close()


def test_prune_by_age_and_max_rows(tmp_path: Path) -> None:
    """Prune deletes rows older than retention_days, then trims to max_rows."""
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path, retention_days=7, max_rows=100)

    now = datetime.now(UTC)

    # Insert 10 old records (beyond retention)
    for i in range(10):
        log.record(
            AuditRecord(
                at=now - timedelta(days=10, seconds=i),
                kind="request",
                scope="user",
                agent_name="agent_old",
                requester_id="@user:example.com",
                method="GET",
                host="api.example.com",
                path=f"/old_{i}",
                service=None,
                status=200,
                bytes_up=i,
                bytes_down=i,
                duration_ms=i,
            ),
        )

    # Insert 5 recent records (within retention)
    for i in range(5):
        log.record(
            AuditRecord(
                at=now - timedelta(days=3, seconds=i),
                kind="request",
                scope="user",
                agent_name="agent_recent",
                requester_id="@user:example.com",
                method="GET",
                host="api.example.com",
                path=f"/recent_{i}",
                service=None,
                status=200,
                bytes_up=i,
                bytes_down=i,
                duration_ms=i,
            ),
        )

    # Prune should delete the 10 old records
    deleted = log.prune()
    assert deleted == 10

    # Verify only 5 recent records remain
    results = log.query(limit=1000)
    assert len(results) == 5
    assert all(r.agent_name == "agent_recent" for r in results)

    # Now test max_rows pruning
    # Insert 200 more recent records
    for i in range(200):
        log.record(
            AuditRecord(
                at=now - timedelta(hours=1, seconds=i),
                kind="request",
                scope="user",
                agent_name="agent_bulk",
                requester_id="@user:example.com",
                method="GET",
                host="api.example.com",
                path=f"/bulk_{i}",
                service=None,
                status=200,
                bytes_up=i,
                bytes_down=i,
                duration_ms=i,
            ),
        )

    # We now have 205 records total (5 recent + 200 bulk)
    # Prune should trim to max_rows=100
    deleted = log.prune()
    assert deleted == 105  # 205 - 100

    # Verify exactly 100 records remain
    results = log.query(limit=1000)
    assert len(results) == 100

    # Verify the newest 100 are kept (all should be agent_bulk since they're most recent)
    assert all(r.agent_name == "agent_bulk" for r in results)

    log.close()


def test_concurrent_record_from_threads(tmp_path: Path) -> None:
    """Concurrent writes from multiple threads all succeed."""
    db_path = tmp_path / "audit.db"
    log = AuditLog(db_path)

    def record_many(thread_id: int) -> None:
        for i in range(100):
            log.record(
                AuditRecord(
                    at=datetime.now(UTC),
                    kind="request",
                    scope="user",
                    agent_name=f"agent_{thread_id}",
                    requester_id="@user:example.com",
                    method="GET",
                    host="api.example.com",
                    path=f"/thread_{thread_id}/record_{i}",
                    service=None,
                    status=200,
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

    log.close()
