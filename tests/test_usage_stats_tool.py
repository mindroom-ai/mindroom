"""Authorization and registration for the retained usage tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.custom_tools.usage_stats import UsageStatsTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.metadata import TOOL_METADATA
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


class _Report:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    def to_dict(self) -> dict[str, object]:
        return {"scope": self.scope, "totals": {"total_tokens": 10}}


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@alice:example.test",
    global_users: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> ToolRuntimeContext:
    config = bind_runtime_paths(
        Config(
            agents={"usage": AgentConfig(display_name="Usage")},
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
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )


def _function_names(toolkit: UsageStatsTools) -> set[str]:
    return set(toolkit.functions) | set(toolkit.async_functions)


@pytest.mark.asyncio
async def test_get_my_usage_uses_bound_agent_and_canonical_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers cannot redirect a self report to another agent or alias identity."""
    context = _context(
        tmp_path,
        requester_id="@telegram-alice:example.test",
        aliases={"@alice:example.test": ["@telegram-alice:example.test"]},
    )
    collect = Mock(return_value=_Report("self"))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_self_usage", collect)

    payload = json.loads(await UsageStatsTools(agent_name="usage").get_my_usage())

    assert payload == {
        "status": "ok",
        "tool": "usage_stats",
        "scope": "self",
        "totals": {"total_tokens": 10},
    }
    assert collect.call_args.kwargs["agent_name"] == "usage"
    assert collect.call_args.kwargs["requester_id"] == "@alice:example.test"
    assert collect.call_args.kwargs["execution_identity"].agent_name == "usage"


def test_admin_function_is_registered_only_when_configured() -> None:
    """The admin endpoint is absent unless the authored override enables it."""
    assert _function_names(UsageStatsTools(agent_name="usage")) == {"get_my_usage"}
    assert _function_names(UsageStatsTools(agent_name="usage", admin_scope=True)) == {
        "get_my_usage",
        "get_all_usage",
    }


def test_registration_keeps_usage_stats_local_and_configurable() -> None:
    """Tool metadata preserves the local primary execution boundary."""
    metadata = TOOL_METADATA["usage_stats"]

    assert metadata.function_names == ("get_my_usage", "get_all_usage")
    assert metadata.default_execution_target.value == "primary"
    assert [field.name for field in metadata.config_fields or []] == ["admin_scope"]


@pytest.mark.asyncio
async def test_admin_rejects_non_global_requester_before_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin-scoped tool still requires a canonical global user."""
    context = _context(tmp_path, requester_id="@outsider:example.test", global_users=["@admin:example.test"])
    collect = Mock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_admin_usage", collect)

    payload = json.loads(await UsageStatsTools(admin_scope=True).get_all_usage())

    assert payload["code"] == "authorization_error"
    collect.assert_not_called()


@pytest.mark.asyncio
async def test_admin_allows_canonical_global_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured global user alias can use the aggregate endpoint."""
    context = _context(
        tmp_path,
        requester_id="@telegram-admin:example.test",
        global_users=["@admin:example.test"],
        aliases={"@admin:example.test": ["@telegram-admin:example.test"]},
    )
    collect = Mock(return_value=_Report("admin"))
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", lambda: context)
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.collect_admin_usage", collect)

    payload = json.loads(
        await UsageStatsTools(admin_scope=True).get_all_usage(
            group_by="entity",
            entity_names=["usage"],
        ),
    )

    assert payload["status"] == "ok"
    assert collect.call_args.kwargs["entity_names"] == ("usage",)


@pytest.mark.asyncio
async def test_invalid_grouping_fails_before_runtime_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid grouping cannot reach caller context or storage."""
    context = Mock()
    monkeypatch.setattr("mindroom.custom_tools.usage_stats.get_tool_runtime_context", context)

    payload = json.loads(await UsageStatsTools().get_my_usage(group_by="entity"))  # type: ignore[arg-type]

    assert payload["code"] == "validation_error"
    context.assert_not_called()
