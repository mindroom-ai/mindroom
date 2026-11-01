"""Month-to-date spend kept across restarts in one SQLite file beside the caps.

The file holds the latest completed scan with a fingerprint of the settings it counted with, the cost of each reply that
finished since that scan started, and the scan requests no completed scan has served yet.
All of it is derived from retained usage, so a store that is missing or unusable costs only a scan.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from mindroom.budgets.caps import budget_caps_path
from mindroom.budgets.pricing import model_prices
from mindroom.budgets.spend import SpendSnapshot, UnpricedModelUsage
from mindroom.constants import resolve_session_state_root

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_SCHEMA_VERSION = 1
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS scan (
    fingerprint TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    scanned_sources INTEGER NOT NULL,
    unavailable_sources INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_spend (user_id TEXT PRIMARY KEY, spend_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scan_unpriced_models (provider TEXT NOT NULL, model TEXT NOT NULL, total_tokens INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reply_costs (recorded_at TEXT NOT NULL, user_id TEXT NOT NULL, cost_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scan_requests (requested_at TEXT NOT NULL);
PRAGMA user_version = {_SCHEMA_VERSION};
"""


@dataclass(frozen=True, slots=True)
class _StoredSpend:
    """The stored scan and its fingerprint, reply costs per requester since it started, and whether a scan is requested."""

    snapshot: SpendSnapshot | None
    fingerprint: str | None
    reply_costs: dict[str, float]
    scan_requested: bool


def spend_store_path(runtime_paths: RuntimePaths) -> Path:
    """Return where one runtime keeps its month-to-date spend."""
    return budget_caps_path(runtime_paths).parent / "spend.sqlite3"


def spend_fingerprint(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return a digest of the settings that change what a scan counts, and of whether budgets are on.

    Prices price usage; the session root and the agents choose which session stores a scan reads; and the agents, teams,
    bot accounts, internal user, and bridge aliases decide whom an alias charges. Caps and the fallback model apply when
    spend is read.
    """
    prices = [
        [provider, model_id, priced.pricing.model_dump(mode="json"), priced.input_includes_cache]
        for (provider, model_id), priced in sorted(model_prices(config).items())
    ]
    payload = [
        config.budgets is not None,
        prices,
        str(resolve_session_state_root(runtime_paths.storage_root, runtime_paths).expanduser().resolve()),
        sorted((name, agent.private is not None) for name, agent in config.agents.items()),
        sorted(config.teams),
        sorted(config.bot_accounts),
        config.mindroom_user.username if config.mindroom_user is not None else None,
        {canonical: sorted(aliases) for canonical, aliases in config.authorization.aliases.items()},
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _timestamp(moment: datetime) -> str:
    """Return a fixed-width UTC timestamp, so stored timestamps compare in time order."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def connect_spend_store(path: Path) -> sqlite3.Connection:
    """Open the store at ``path``, creating it when missing, or raise ``sqlite3.DatabaseError`` for another schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only the monitor's one writer thread writes, so a lock held for long is stuck, and only delays a start.
    connection = sqlite3.connect(path, timeout=1)
    try:
        _prepare(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def _prepare(connection: sqlite3.Connection, path: Path) -> None:
    # The storage root may be a network filesystem, where WAL is unsafe, and a stored reply cost must survive a reset.
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        connection.executescript(_SCHEMA)
    elif version != _SCHEMA_VERSION:
        msg = f"Budget spend store {path} has schema version {version}, expected {_SCHEMA_VERSION}"
        raise sqlite3.DatabaseError(msg)


def discard_spend_store(path: Path) -> None:
    """Delete the store at ``path`` with any rollback journal, which SQLite would otherwise replay into the next one."""
    for name in (path.name, f"{path.name}-journal"):
        path.with_name(name).unlink(missing_ok=True)


def load_spend(path: Path) -> _StoredSpend:
    """Return what the store at ``path`` holds."""
    with closing(connect_spend_store(path)) as connection:
        scan = connection.execute(
            "SELECT fingerprint, period_start, period_end, generated_at, scanned_sources, unavailable_sources FROM scan",
        ).fetchone()
        snapshot = (
            None
            if scan is None
            else SpendSnapshot(
                period_start=date.fromisoformat(scan[1]),
                period_end=date.fromisoformat(scan[2]),
                generated_at=datetime.fromisoformat(scan[3]),
                spend_usd=dict(connection.execute("SELECT user_id, spend_usd FROM scan_spend")),
                unpriced_models=tuple(
                    UnpricedModelUsage(provider=provider, model=model, total_tokens=tokens)
                    for provider, model, tokens in connection.execute(
                        "SELECT provider, model, total_tokens FROM scan_unpriced_models ORDER BY rowid",
                    )
                ),
                scanned_sources=scan[4],
                unavailable_sources=scan[5],
            )
        )
        return _StoredSpend(
            snapshot=snapshot,
            fingerprint=None if scan is None else scan[0],
            reply_costs=dict(connection.execute("SELECT user_id, SUM(cost_usd) FROM reply_costs GROUP BY user_id")),
            scan_requested=connection.execute("SELECT EXISTS (SELECT 1 FROM scan_requests)").fetchone()[0] == 1,
        )


def store_scan(connection: sqlite3.Connection, snapshot: SpendSnapshot, fingerprint: str) -> None:
    """Replace the stored scan with ``snapshot``, and drop the reply costs and scan requests it covers.

    A scan's ``generated_at`` is the time it started, which is the ``now`` it dates usage by, and it covers everything
    recorded until then.
    """
    started_at = _timestamp(snapshot.generated_at)
    with connection:
        connection.execute("DELETE FROM scan")
        connection.execute(
            "INSERT INTO scan VALUES (?, ?, ?, ?, ?, ?)",
            (
                fingerprint,
                snapshot.period_start.isoformat(),
                snapshot.period_end.isoformat(),
                snapshot.generated_at.isoformat(),
                snapshot.scanned_sources,
                snapshot.unavailable_sources,
            ),
        )
        connection.execute("DELETE FROM scan_spend")
        connection.executemany("INSERT INTO scan_spend VALUES (?, ?)", snapshot.spend_usd.items())
        connection.execute("DELETE FROM scan_unpriced_models")
        connection.executemany(
            "INSERT INTO scan_unpriced_models VALUES (?, ?, ?)",
            ((row.provider, row.model, row.total_tokens) for row in snapshot.unpriced_models),
        )
        connection.execute("DELETE FROM reply_costs WHERE recorded_at <= ?", (started_at,))
        connection.execute("DELETE FROM scan_requests WHERE requested_at <= ?", (started_at,))


def add_reply_cost(connection: sqlite3.Connection, user_id: str, cost_usd: float, recorded_at: datetime) -> None:
    """Store one finished reply's cost for its canonical requester."""
    with connection:
        connection.execute("INSERT INTO reply_costs VALUES (?, ?, ?)", (_timestamp(recorded_at), user_id, cost_usd))


def add_scan_request(connection: sqlite3.Connection, requested_at: datetime) -> None:
    """Store a scan request until a scan that started after it completes."""
    with connection:
        connection.execute("INSERT INTO scan_requests VALUES (?)", (_timestamp(requested_at),))
