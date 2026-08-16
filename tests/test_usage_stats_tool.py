"""Runtime authorization tests for the retained usage statistics toolkit."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.custom_tools.usage_stats import UsageStatsTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext
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
