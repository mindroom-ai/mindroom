"""Runtime authorization tests for the retained usage statistics toolkit."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, Mock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.custom_tools.usage_stats import UsageStatsTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.metadata import TOOL_METADATA, export_tools_metadata, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from mindroom.usage_stats import (
    CostCoverage,
    TokenTotals,
    UsageBreakdownRow,
    UsageCoverage,
    UsageReport,
    UsageStatsSourceUnavailableError,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@alice:example.test",
    global_users: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(
            agents={"usage": AgentConfig(display_name="Usage Agent")},
            authorization=AuthorizationConfig(
                global_users=global_users or [],
                aliases=aliases or {},
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    return ToolRuntimeContext(
        agent_name="usage",
        target=MessageTarget.resolve(
            room_id="!usage:example.test",
            thread_id="$usage-thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )


def _report_payload(scope: str) -> dict[str, object]:
    return {
        "scope": scope,
        "window": {"start": None, "end": "2026-08-16T00:00:00+00:00", "timezone": "UTC"},
        "breakdown": [{"dimension": "day", "key": "2026-08-15"}],
    }


def _registered_function_names(toolkit: UsageStatsTools) -> set[str]:
    return set(toolkit.functions) | set(toolkit.async_functions)


class _Report:
    def __init__(self, scope: str) -> None:
        self._payload = _report_payload(scope)

    def to_dict(self) -> dict[str, object]:
        return self._payload


def _public_report(
    scope: Literal["self", "admin"],
    dimension: Literal["model", "entity"],
) -> UsageReport:
    """Build a representative public report without persisted run data."""
    totals = TokenTotals(input_tokens=1, output_tokens=2, total_tokens=3)
    cost = CostCoverage(known_cost="0.01", runs_with_cost=1, runs_without_cost=0)
    return UsageReport(
        scope=scope,
        start=None,
        end="2026-08-16T00:00:00+00:00",
        timezone="UTC",
        as_of="2026-08-16T00:00:00+00:00",
        totals=totals,
        cost=cost,
        turn_count=1,
        run_count=1,
        session_count=1,
        first_observed_at="2026-08-15T00:00:00+00:00",
        last_observed_at="2026-08-15T00:00:00+00:00",
        status_counts={"completed": 1},
        breakdown=(
            UsageBreakdownRow(
                dimension=dimension,
                key="usage",
                model_type="chat" if dimension == "model" else None,
                provider="openai" if dimension == "model" else None,
                model_id="gpt-5.6" if dimension == "model" else None,
                totals=totals,
                cost=cost,
                run_count=1,
            ),
        ),
        breakdown_truncated=False,
        breakdown_omitted=0,
        coverage=UsageCoverage(
            status="complete_retained",
            scanned_sources=1,
            partial_sources=0,
            scanned_sessions=1,
            retained_runs=1,
            skipped_runs=0,
            malformed_runs=0,
            missing_requester_runs=0,
            missing_timestamp_runs=0,
            compacted_sessions=0,
            note="No retained-history gap was detected.",
        ),
    )


@pytest.mark.asyncio
async def test_get_my_usage_derives_canonical_runtime_identity_before_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied agent or requester must not redirect a self usage read."""
    context = _context(
        tmp_path,
        requester_id="@telegram-alice:example.test",
        aliases={"@alice:example.test": ["@telegram-alice:example.test"]},
    )
    identity = object()
    collect = Mock(return_value=_Report("self"))
    to_thread = AsyncMock(side_effect=lambda function, **kwargs: function(**kwargs))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.usage_stats.build_execution_identity_from_runtime_context",
        lambda received: identity if received is context else None,
    )
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.asyncio.to_thread", to_thread)

    payload = json.loads(await UsageStatsTools().get_my_usage(group_by="model"))

    assert list(inspect.signature(UsageStatsTools.get_my_usage).parameters) == ["self", "start", "end", "group_by"]
    to_thread.assert_awaited_once()
    assert to_thread.await_args.args == (collect,)
    assert collect.call_args.kwargs == {
        "agent_name": "usage",
        "requester_id": "@alice:example.test",
        "config": context.config,
        "runtime_paths": context.runtime_paths,
        "execution_identity": identity,
        "start": None,
        "end": None,
        "group_by": "model",
    }
    assert payload == {"status": "ok", "tool": "usage_stats", **_report_payload("self")}
    assert "@telegram-alice:example.test" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("context_available", [False, True])
async def test_get_my_usage_rejects_missing_context_or_requester_before_identity_or_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    context_available: bool,
) -> None:
    """Missing caller identity must fail closed before any usage source can be discovered."""
    context = replace(_context(tmp_path), requester_id="") if context_available else None
    identity_builder = Mock()
    collect = Mock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        "mindroom.custom_tools.usage_stats.build_execution_identity_from_runtime_context",
        identity_builder,
    )
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect)

    payload = json.loads(await UsageStatsTools().get_my_usage())

    assert payload == {
        "code": "context_unavailable",
        "message": "Usage statistics are unavailable without an active requester context.",
        "status": "error",
        "tool": "usage_stats",
    }
    identity_builder.assert_not_called()
    collect.assert_not_called()


def test_usage_stats_dynamic_registration_follows_admin_scope() -> None:
    """An accidental aggregate endpoint registration would bypass authored scope configuration."""
    assert _registered_function_names(UsageStatsTools()) == {"get_my_usage"}
    assert _registered_function_names(UsageStatsTools(admin_scope=True)) == {"get_my_usage", "get_all_usage"}


def test_usage_stats_registration_exposes_the_authored_primary_scope() -> None:
    """Changing registration metadata could make an admin endpoint remotely executable or unconfigurable."""
    metadata = TOOL_METADATA["usage_stats"]

    assert metadata.function_names == ("get_my_usage", "get_all_usage")
    assert metadata.default_execution_target.value == "primary"
    assert [field.name for field in metadata.config_fields or []] == ["admin_scope"]
    assert [field.name for field in metadata.agent_override_fields or []] == ["admin_scope"]


def test_usage_stats_export_and_payloads_expose_only_the_documented_public_shape() -> None:
    """Dashboard metadata and JSON payloads must retain the documented safe contract."""
    metadata = next(tool for tool in export_tools_metadata() if tool["name"] == "usage_stats")

    assert metadata["function_names"] == ("get_my_usage", "get_all_usage")
    assert metadata["default_execution_target"] == "primary"
    assert [field["name"] for field in metadata["config_fields"]] == ["admin_scope"]
    assert [field["default"] for field in metadata["config_fields"]] == [False]
    assert [field["name"] for field in metadata["agent_override_fields"]] == ["admin_scope"]
    assert [field["default"] for field in metadata["agent_override_fields"]] == [False]
    assert [field["authored_override"] for field in metadata["agent_override_fields"]] == [True]

    expected_payload_keys = {
        "status",
        "tool",
        "scope",
        "window",
        "as_of",
        "totals",
        "cost",
        "turn_count",
        "run_count",
        "session_count",
        "first_observed_at",
        "last_observed_at",
        "status_counts",
        "breakdown",
        "breakdown_truncated",
        "breakdown_omitted",
        "coverage",
    }
    expected_totals_keys = {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "audio_input_tokens",
        "audio_output_tokens",
        "audio_total_tokens",
    }
    expected_cost_keys = {"known_cost", "runs_with_cost", "runs_without_cost"}
    expected_coverage_keys = {
        "status",
        "scanned_sources",
        "partial_sources",
        "scanned_sessions",
        "retained_runs",
        "skipped_runs",
        "malformed_runs",
        "missing_requester_runs",
        "missing_timestamp_runs",
        "compacted_sessions",
        "note",
    }

    for scope, dimension in (("self", "model"), ("admin", "entity")):
        payload = json.loads(UsageStatsTools._payload("ok", **_public_report(scope, dimension).to_dict()))

        assert set(payload) == expected_payload_keys
        assert payload["scope"] == scope
        assert set(payload["window"]) == {"start", "end", "timezone"}
        assert set(payload["totals"]) == expected_totals_keys
        assert set(payload["cost"]) == expected_cost_keys
        assert set(payload["coverage"]) == expected_coverage_keys
        assert set(payload["breakdown"][0]) <= {
            "dimension",
            "key",
            "model",
            "totals",
            "cost",
            "run_count",
        }
        assert set(payload["breakdown"][0]["totals"]) == expected_totals_keys
        assert set(payload["breakdown"][0]["cost"]) == expected_cost_keys


@pytest.mark.asyncio
async def test_get_all_usage_rejects_non_global_requester_before_thread_or_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The constructor gate alone must not grant aggregate retained-history access."""
    context = _context(tmp_path, requester_id="@outsider:example.test", global_users=["@admin:example.test"])
    collect = Mock()
    to_thread = AsyncMock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_admin_usage", collect)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.asyncio.to_thread", to_thread)

    payload = json.loads(await UsageStatsTools(admin_scope=True).get_all_usage())

    assert payload == {
        "code": "authorization_error",
        "message": "Usage statistics admin access is not authorized for this requester.",
        "status": "error",
        "tool": "usage_stats",
    }
    to_thread.assert_not_awaited()
    collect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requester_id", "aliases"),
    [
        ("@admin:example.test", {}),
        ("@telegram-admin:example.test", {"@admin:example.test": ["@telegram-admin:example.test"]}),
    ],
)
async def test_get_all_usage_allows_canonical_or_alias_global_requester_in_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requester_id: str,
    aliases: dict[str, list[str]],
) -> None:
    """Canonical global membership must authorize both Matrix and bridged callers."""
    context = _context(
        tmp_path,
        requester_id=requester_id,
        global_users=["@admin:example.test"],
        aliases=aliases,
    )
    collect = Mock(return_value=_Report("admin"))
    to_thread = AsyncMock(side_effect=lambda function, **kwargs: function(**kwargs))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_admin_usage", collect)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.asyncio.to_thread", to_thread)

    payload = json.loads(
        await UsageStatsTools(admin_scope=True).get_all_usage(
            group_by="requester",
            entity_names=["usage"],
            requester_ids=["@telegram-admin:example.test"],
        ),
    )

    to_thread.assert_awaited_once()
    assert to_thread.await_args.args == (collect,)
    assert to_thread.await_args.kwargs == {
        "config": context.config,
        "runtime_paths": context.runtime_paths,
        "start": None,
        "end": None,
        "group_by": "requester",
        "entity_names": ("usage",),
        "requester_ids": ("@telegram-admin:example.test",),
    }
    assert payload == {"status": "ok", "tool": "usage_stats", **_report_payload("admin")}
    assert "@admin:example.test" not in json.dumps(payload)


def test_usage_stats_agent_override_reaches_constructor(tmp_path: Path) -> None:
    """A per-agent admin_scope override must control the registered toolkit surface."""
    config = bind_runtime_paths(
        Config(
            agents={
                "usage": AgentConfig(
                    display_name="Usage Agent",
                    tools=[{"name": "usage_stats", "overrides": {"admin_scope": True}}],
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    usage_tool_config = next(
        entry for entry in config.resolve_entity("usage").tool_configs if entry.name == "usage_stats"
    )

    toolkit = get_tool_by_name(
        "usage_stats",
        runtime_paths_for(config),
        tool_config_overrides=usage_tool_config.tool_config_overrides,
        worker_target=None,
    )

    assert isinstance(toolkit, UsageStatsTools)
    assert _registered_function_names(toolkit) == {"get_my_usage", "get_all_usage"}


@pytest.mark.asyncio
async def test_public_group_by_validation_returns_stable_code_before_context_or_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported runtime literal cannot silently become a day breakdown or touch context."""
    context = Mock()
    collect_self = Mock()
    collect_admin = Mock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect_self)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_admin_usage", collect_admin)

    self_payload = json.loads(await UsageStatsTools().get_my_usage(group_by="entity"))  # type: ignore[arg-type]
    admin_payload = json.loads(
        await UsageStatsTools(admin_scope=True).get_all_usage(group_by="week"),  # type: ignore[arg-type]
    )

    expected = {
        "code": "validation_error",
        "message": "Unsupported usage statistics grouping.",
        "status": "error",
        "tool": "usage_stats",
    }
    assert self_payload == expected
    assert admin_payload == expected
    context.assert_not_called()
    collect_self.assert_not_called()
    collect_admin.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("end", "message"),
    [
        ("9999-12-31", "Invalid date"),
        ("0001-01-01T00:00:00+14:00", "Invalid timestamp"),
    ],
)
async def test_public_boundary_overflow_returns_validation_error_before_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end: str,
    message: str,
) -> None:
    """Caller-controlled boundary overflow must remain a coded validation error without scanning storage."""
    context = _context(tmp_path, global_users=["@alice:example.test"])
    discover = Mock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.usage_stats.discover_admin_usage_sources", discover)

    payload = json.loads(await UsageStatsTools(admin_scope=True).get_all_usage(end=end))

    assert payload == {
        "code": "validation_error",
        "message": message,
        "status": "error",
        "tool": "usage_stats",
    }
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_source_unavailable_error_has_stable_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expected unreadable self source maps to its distinct aggregate-only error."""
    context = _context(tmp_path)
    collect = Mock(side_effect=UsageStatsSourceUnavailableError("private path must not leak"))
    to_thread = AsyncMock(side_effect=lambda function, **kwargs: function(**kwargs))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.asyncio.to_thread", to_thread)

    payload = json.loads(await UsageStatsTools().get_my_usage())

    assert payload == {
        "code": "source_unavailable",
        "message": "Usage statistics could not read the expected retained-history source.",
        "status": "error",
        "tool": "usage_stats",
    }
    assert "private path" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_unexpected_collection_error_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Programming defects must reach runtime logging instead of becoming fabricated zero usage."""
    context = _context(tmp_path)
    collect = Mock(side_effect=RuntimeError("programming defect"))
    to_thread = AsyncMock(side_effect=lambda function, **kwargs: function(**kwargs))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.asyncio.to_thread", to_thread)

    with pytest.raises(RuntimeError, match="programming defect"):
        await UsageStatsTools().get_my_usage()
