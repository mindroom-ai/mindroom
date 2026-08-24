"""Silent scheduled-run receipt routing and schema tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.scheduled_run_records import (
    record_silent_schedule_result_if_needed,
    record_silent_schedule_started_if_needed,
)
from tests.conftest import request_envelope, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


def _config(*, agents: dict[str, AgentConfig], teams: dict[str, TeamConfig] | None = None) -> Config:
    return Config(
        agents=agents,
        teams=teams or {},
        models={"default": ModelConfig(provider="test", id="test-model")},
    )


async def test_team_silent_run_writes_one_receipt_per_member_workspace(tmp_path: Path) -> None:
    """A configured team leaves independently discoverable evidence for every member agent."""
    config = _config(
        agents={
            "alpha": AgentConfig(display_name="Alpha"),
            "beta": AgentConfig(display_name="Beta"),
        },
        teams={
            "watchers": TeamConfig(
                display_name="Watchers",
                role="Check for changes",
                agents=["alpha", "beta"],
            ),
        },
    )
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$team-run",
        prompt="Check both systems",
        agent_name="watchers",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )

    await record_silent_schedule_started_if_needed(
        entity_name="watchers",
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )

    for agent_name in ("alpha", "beta"):
        receipts = list(
            (runtime_paths.storage_root / "agents" / agent_name / "workspace" / ".mindroom" / "scheduled_runs").glob(
                "*.json",
            ),
        )
        assert len(receipts) == 1
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["entity_name"] == "watchers"
        assert receipt["agent_name"] == agent_name
        assert receipt["status"] == "started"


async def test_private_agent_silent_run_uses_requester_scoped_workspace(tmp_path: Path) -> None:
    """Private receipts stay with the requester materialization instead of shared agent state."""
    config = _config(
        agents={
            "private": AgentConfig(
                display_name="Private",
                private=AgentPrivateConfig(per="user", root="private_data"),
            ),
        },
    )
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$private-run",
        prompt="Check my private inbox",
        user_id="@alice:localhost",
        agent_name="private",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )

    await record_silent_schedule_started_if_needed(
        entity_name="private",
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    await record_silent_schedule_result_if_needed(
        entity_name="private",
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason="silent_no_report",
        response_text="NO_REPLY",
    )

    receipts = list(
        runtime_paths.storage_root.glob(
            "private_instances/*/private/private_data/.mindroom/scheduled_runs/*.json",
        ),
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["agent_name"] == "private"
    assert receipt["result"] == "no_report"
    assert receipt["status"] == "completed"
    assert not (runtime_paths.storage_root / "agents" / "private" / "workspace" / ".mindroom").exists()
