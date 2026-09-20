"""Voice duration uses the same durable, caller-owned storage as delegated replies."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_storage import save_independent_usage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.call_tools import record_call_voice_usage
from mindroom.matrix_rtc.voice_agent import LiveVoiceUsage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_private_usage
from tests.conftest import test_runtime_paths
from tests.test_request_usage import request_usage  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.sqlite import SqliteDb

    from mindroom.constants import RuntimePaths


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_voice_usage_keeps_caller_ownership(tmp_path: Path, private: bool) -> None:
    """A shared default store or lost alias would misattribute paid voice time."""
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                private=AgentPrivateConfig(per="user", root="mind_data") if private else None,
            ),
        },
        authorization=AuthorizationConfig(aliases={"@alice:example.org": ["@alias:example.org"]}),
    )
    for requester in ("@alias:example.org", "@bob:example.org"):
        identity = ToolExecutionIdentity(
            channel="matrix",
            agent_name="helper",
            requester_id=requester,
            room_id="!room:example.org",
            thread_id=None,
            resolved_thread_id=None,
            session_id="call",
        )
        usage = LiveVoiceUsage(f"provider-{requester}", "gpt-live-1", 1_700_000_000, 12.5, True)
        await record_call_voice_usage(usage, config=config, runtime_paths=paths, execution_identity=identity)

    report = collect_admin_usage(config=config, runtime_paths=paths).to_dict()
    assert [(row["user_id"], row["duration_seconds"]) for row in report["voice_breakdown"]] == [
        ("@alice:example.org", 12.5),
        ("@bob:example.org", 12.5),
    ]
    assert report["totals"]["total_tokens"] == 0
    if private:
        assert len(list(paths.storage_root.rglob("helper.db"))) == 2
    personal = collect_private_usage(requester_id="@bob:example.org", config=config, runtime_paths=paths).to_dict()
    assert "voice_breakdown" not in personal


@pytest.mark.parametrize("seconds", [-1, True, "12", math.nan, math.inf, None, pytest.param(10**400, id="overflow")])
def test_invalid_voice_duration_is_excluded_without_discarding_delegate_tokens(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],  # noqa: F811
    seconds: object,
) -> None:
    """Malformed voice records cannot become billable seconds or erase sound token counters."""
    config, paths, storage = request_usage
    save_independent_usage(
        storage,
        session_id="session",
        usage_id="live_voice:invalid",
        kind="live_voice",
        requester_id="@alice:example.test",
        run={
            "model_provider": "OpenAI",
            "model": "gpt-live-1",
            "created_at": 1_700_000_000,
            "voice_seconds": seconds,
            "voice_finalized": False,
        },
    )
    report = collect_admin_usage(config=config, runtime_paths=paths).to_dict()
    assert report["voice_breakdown"] == []
    assert report["voice_coverage"]["unavailable_sources"] == 1
    assert report["totals"]["total_tokens"] == 250_014
