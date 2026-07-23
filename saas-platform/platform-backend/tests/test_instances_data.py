"""Unit tests for the instances-table data-access service."""

from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

from backend.services import instances_data


@dataclass
class StubResult:
    """Mimics the Supabase execute() response."""

    data: Any


class StubQuery:
    """Records the Supabase query-builder chain and returns canned data."""

    def __init__(self, data: Any, calls: list[tuple[str, Any]]) -> None:
        self._data = data
        self._calls = calls

    def select(self, columns: str) -> "StubQuery":
        self._calls.append(("select", columns))
        return self

    def insert(self, fields: dict[str, Any]) -> "StubQuery":
        self._calls.append(("insert", fields))
        return self

    def update(self, fields: dict[str, Any]) -> "StubQuery":
        self._calls.append(("update", fields))
        return self

    def eq(self, column: str, value: Any) -> "StubQuery":
        self._calls.append(("eq", (column, value)))
        return self

    def order(self, column: str, desc: bool = False) -> "StubQuery":  # noqa: FBT001, FBT002
        self._calls.append(("order", (column, desc)))
        return self

    def limit(self, count: int) -> "StubQuery":
        self._calls.append(("limit", count))
        return self

    def execute(self) -> StubResult:
        self._calls.append(("execute", None))
        return StubResult(self._data)


class StubSupabase:
    """Stub Supabase client returning the same canned data for every query."""

    def __init__(self, data: Any) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._data = data

    def table(self, name: str) -> StubQuery:
        self.calls.append(("table", name))
        return StubQuery(self._data, self.calls)


class FailingSupabase:
    """Stub Supabase client whose queries always raise."""

    def table(self, name: str) -> "FailingSupabase":  # noqa: ARG002
        return self

    def __getattr__(self, name: str) -> Any:
        msg = "supabase unavailable"
        raise RuntimeError(msg)


ROW_A = {"id": 1, "instance_id": "123", "account_id": "acc-1", "status": "running"}
ROW_B = {"id": 2, "instance_id": "456", "account_id": "acc-1", "status": "stopped"}


class TestGetInstance:
    def test_returns_first_row(self):
        sb = StubSupabase([ROW_A, ROW_B])
        assert instances_data.get_instance(sb, 123) == ROW_A
        assert sb.calls == [
            ("table", "instances"),
            ("select", "*"),
            ("eq", ("instance_id", "123")),
            ("execute", None),
        ]

    def test_returns_none_when_absent(self):
        assert instances_data.get_instance(StubSupabase([]), "123") is None

    def test_selects_requested_columns(self):
        sb = StubSupabase([{"status": "running"}])
        assert instances_data.get_instance(sb, 123, columns="status") == {"status": "running"}
        assert ("select", "status") in sb.calls


class TestGetOwnedInstance:
    def test_filters_by_instance_and_account(self):
        sb = StubSupabase([ROW_A])
        assert instances_data.get_owned_instance(sb, 123, "acc-1") == ROW_A
        assert sb.calls == [
            ("table", "instances"),
            ("select", "id,instance_id,subscription_id,account_id"),
            ("eq", ("instance_id", "123")),
            ("eq", ("account_id", "acc-1")),
            ("limit", 1),
            ("execute", None),
        ]

    def test_returns_none_when_not_owned(self):
        assert instances_data.get_owned_instance(StubSupabase([]), "123", "acc-2") is None


class TestGetInstancesForAccount:
    def test_returns_all_rows(self):
        sb = StubSupabase([ROW_A, ROW_B])
        assert instances_data.get_instances_for_account(sb, "acc-1") == [ROW_A, ROW_B]
        assert ("eq", ("account_id", "acc-1")) in sb.calls
        assert all(call[0] != "order" for call in sb.calls)

    def test_newest_first_orders_by_created_at_desc(self):
        sb = StubSupabase([ROW_B, ROW_A])
        assert instances_data.get_instances_for_account(sb, "acc-1", newest_first=True) == [ROW_B, ROW_A]
        assert ("order", ("created_at", True)) in sb.calls

    def test_returns_empty_list_when_data_is_none(self):
        assert instances_data.get_instances_for_account(StubSupabase(None), "acc-1") == []


class TestListInstances:
    def test_selects_all_columns_by_default(self):
        sb = StubSupabase([ROW_A, ROW_B])
        assert instances_data.list_instances(sb) == [ROW_A, ROW_B]
        assert ("select", "*") in sb.calls

    def test_selects_column_subset(self):
        sb = StubSupabase([{"status": "running"}])
        assert instances_data.list_instances(sb, columns="status") == [{"status": "running"}]
        assert ("select", "status") in sb.calls

    def test_returns_empty_list_when_data_is_none(self):
        assert instances_data.list_instances(StubSupabase(None)) == []


class TestCreateInstance:
    def test_returns_inserted_row(self):
        sb = StubSupabase([ROW_A])
        fields = {"account_id": "acc-1", "status": "provisioning"}
        assert instances_data.create_instance(sb, fields) == ROW_A
        assert sb.calls[:2] == [("table", "instances"), ("insert", fields)]

    def test_returns_none_when_insert_returned_no_rows(self):
        assert instances_data.create_instance(StubSupabase([]), {"account_id": "acc-1"}) is None


class TestUpdateInstance:
    def test_stamps_updated_at_and_returns_rows(self):
        sb = StubSupabase([ROW_A])
        rows = instances_data.update_instance(sb, 123, {"status": "running"})
        assert rows == [ROW_A]
        update_payload = next(payload for call, payload in sb.calls if call == "update")
        assert update_payload["status"] == "running"
        assert "updated_at" in update_payload
        assert ("eq", ("instance_id", "123")) in sb.calls

    def test_caller_provided_updated_at_wins(self):
        sb = StubSupabase([ROW_A])
        instances_data.update_instance(sb, "123", {"status": "stopped", "updated_at": "2026-01-01T00:00:00+00:00"})
        update_payload = next(payload for call, payload in sb.calls if call == "update")
        assert update_payload["updated_at"] == "2026-01-01T00:00:00+00:00"

    def test_returns_empty_list_when_no_rows_matched(self):
        assert instances_data.update_instance(StubSupabase([]), "123", {"status": "error"}) == []


class TestUpdateInstanceStatus:
    def test_returns_true_on_success(self):
        sb = StubSupabase([ROW_A])
        with patch("backend.services.instances_data.ensure_supabase", return_value=sb):
            assert instances_data.update_instance_status(123, "running") is True
        update_payload = next(payload for call, payload in sb.calls if call == "update")
        assert update_payload["status"] == "running"

    def test_returns_false_when_query_raises(self):
        with patch("backend.services.instances_data.ensure_supabase", return_value=FailingSupabase()):
            assert instances_data.update_instance_status(123, "running") is False

    def test_returns_false_when_supabase_unconfigured(self):
        with patch("backend.services.instances_data.ensure_supabase", side_effect=RuntimeError("not configured")):
            assert instances_data.update_instance_status(123, "running") is False
