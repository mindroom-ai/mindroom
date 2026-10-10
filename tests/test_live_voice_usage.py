"""Voice duration uses the same durable, caller-owned storage as delegated replies."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_storage import save_independent_usage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.call_tools import record_call_token_usage, record_call_voice_usage
from mindroom.matrix_rtc.voice_agent import LiveVoiceUsage, RealtimeCallUsage
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


@pytest.mark.asyncio
async def test_realtime_call_tokens_count_once_for_the_caller(tmp_path: Path) -> None:
    """Realtime speech models bill tokens; each update replaces the call's running totals."""
    paths = test_runtime_paths(tmp_path)
    config = Config(agents={"helper": AgentConfig(display_name="Helper")})
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="helper",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="call",
    )
    for usage in (
        RealtimeCallUsage(
            usage_id="call-1",
            model="gpt-realtime-2.1",
            created_at=1_700_000_000,
            input_tokens=900,
            output_tokens=300,
            cache_read_tokens=400,
            audio_input_tokens=800,
            audio_output_tokens=250,
        ),
        RealtimeCallUsage(
            usage_id="call-1",
            model="gpt-realtime-2.1",
            created_at=1_700_000_000,
            input_tokens=2000,
            output_tokens=700,
            cache_read_tokens=1200,
            audio_input_tokens=1800,
            audio_output_tokens=600,
        ),
    ):
        await record_call_token_usage(usage, config=config, runtime_paths=paths, execution_identity=identity)

    report = collect_admin_usage(config=config, runtime_paths=paths)
    totals = report.totals
    assert (totals.input_tokens, totals.output_tokens, totals.cache_read_tokens) == (2000, 700, 1200)
    assert (totals.audio_input_tokens, totals.audio_output_tokens) == (1800, 600)
    assert [(row.user_id, row.totals.total_tokens) for row in report.user_breakdown] == [("@alice:example.org", 2700)]
    assert [(row.model, row.totals.total_tokens) for row in report.model_breakdown] == [("gpt-realtime-2.1", 2700)]


def test_voice_duration_without_caller_keeps_usage_but_reports_incomplete_coverage(
    request_usage: tuple[Config, RuntimePaths, SqliteDb],  # noqa: F811
) -> None:
    """Known voice duration must survive missing attribution without claiming complete coverage."""
    config, paths, storage = request_usage
    save_independent_usage(
        storage,
        session_id="session",
        usage_id="live_voice:unattributed",
        kind="live_voice",
        requester_id=None,
        run={
            "model_provider": "OpenAI",
            "model": "gpt-live-1",
            "created_at": 1_700_000_000,
            "voice_seconds": 12.5,
            "voice_finalized": True,
        },
    )
    report = collect_admin_usage(config=config, runtime_paths=paths).to_dict()
    assert [(row["user_id"], row["duration_seconds"]) for row in report["voice_breakdown"]] == [(None, 12.5)]
    assert report["voice_coverage"]["unavailable_sources"] == 1
    assert report["totals"]["total_tokens"] == 250_014


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
