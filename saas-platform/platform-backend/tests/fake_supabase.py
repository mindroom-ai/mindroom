"""In-memory stand-in for the subset of the Supabase query builder the backend uses.

Rows are plain dicts per table. Filters compare values as strings, like PostgREST query parameters.
Embedded many-to-one selects such as ``subscription:subscriptions(*)`` resolve through ``<table>_id`` columns.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

_EMBED = re.compile(r"(\w+):(\w+)\(([^)]*)\)")
# Embedded table -> foreign key column on the parent row.
_FOREIGN_KEYS = {"subscriptions": "subscription_id", "accounts": "account_id"}


@dataclass
class FakeResult:
    """Query result exposing ``data`` like the Supabase client."""

    data: Any


@dataclass
class FakeQuery:
    """One chained query against a fake table."""

    db: FakeSupabase
    table_name: str
    action: str = "select"
    columns: str = "*"
    payload: dict[str, Any] | None = None
    filters: list[tuple[str, str, Any]] = field(default_factory=list)
    order_by: tuple[str, bool] | None = None
    row_range: tuple[int, int] | None = None
    row_limit: int | None = None
    single_row: bool = False
    conflict_column: str | None = None

    def select(self, columns: str = "*", **_kwargs: Any) -> FakeQuery:  # noqa: ANN401
        self.columns = columns
        return self

    def update(self, payload: dict[str, Any]) -> FakeQuery:
        self.action, self.payload = "update", payload
        return self

    def insert(self, payload: dict[str, Any]) -> FakeQuery:
        self.action, self.payload = "insert", payload
        return self

    def upsert(self, payload: dict[str, Any], *, on_conflict: str) -> FakeQuery:
        self.action, self.payload, self.conflict_column = "upsert", payload, on_conflict
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:  # noqa: ANN401
        self.filters.append(("eq", column, value))
        return self

    def in_(self, column: str, values: list[Any]) -> FakeQuery:
        self.filters.append(("in", column, values))
        return self

    def order(self, column: str, *, desc: bool = False) -> FakeQuery:
        self.order_by = (column, desc)
        return self

    def range(self, start: int, end: int) -> FakeQuery:
        self.row_range = (start, end)
        return self

    def limit(self, count: int) -> FakeQuery:
        self.row_limit = count
        return self

    def single(self) -> FakeQuery:
        self.single_row = True
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        for op, column, value in self.filters:
            if op == "eq" and str(row.get(column)) != str(value):
                return False
            if op == "in" and str(row.get(column)) not in {str(v) for v in value}:
                return False
        return True

    def _project(self, row: dict[str, Any]) -> dict[str, Any]:
        projected = dict(row)
        for alias, table, _columns in _EMBED.findall(self.columns):
            key = row.get(_FOREIGN_KEYS[table])
            match = next((r for r in self.db.tables.get(table, []) if str(r["id"]) == str(key)), None)
            projected[alias] = dict(match) if match else None
        return projected

    def execute(self) -> FakeResult:
        rows = self.db.tables.setdefault(self.table_name, [])
        if self.action == "insert":
            assert self.payload is not None
            row = {"id": str(uuid.uuid4()), **self.payload}
            rows.append(row)
            return FakeResult([dict(row)])
        if self.action == "upsert":
            assert self.payload is not None
            assert self.conflict_column is not None
            key = str(self.payload[self.conflict_column])
            existing = next((row for row in rows if str(row.get(self.conflict_column)) == key), None)
            if existing is None:
                existing = {"id": str(uuid.uuid4())}
                rows.append(existing)
            existing.update(self.payload)
            return FakeResult([dict(existing)])
        matched = [row for row in rows if self._matches(row)]
        if self.action == "update":
            assert self.payload is not None
            for row in matched:
                row.update(self.payload)
            return FakeResult([dict(row) for row in matched])
        if self.order_by:
            column, desc = self.order_by
            matched.sort(key=lambda row: str(row.get(column) or ""), reverse=desc)
        if self.row_range:
            matched = matched[self.row_range[0] : self.row_range[1] + 1]
        if self.row_limit is not None:
            matched = matched[: self.row_limit]
        projected = [self._project(row) for row in matched]
        if self.single_row:
            assert len(projected) == 1, f"single() matched {len(projected)} rows in {self.table_name}"
            return FakeResult(projected[0])
        return FakeResult(projected)


@dataclass
class FakeSupabase:
    """Minimal Supabase client backed by per-table row lists."""

    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def row(self, table: str, **match: Any) -> dict[str, Any]:  # noqa: ANN401
        """Return the single row matching every given column."""
        rows = [r for r in self.tables.get(table, []) if all(str(r.get(k)) == str(v) for k, v in match.items())]
        assert len(rows) == 1, rows
        return rows[0]
