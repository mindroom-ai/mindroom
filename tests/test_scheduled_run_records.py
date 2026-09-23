"""Silent scheduled-run receipt routing and schema tests."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom import scheduled_run_records
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
        agent_names=("alpha", "beta"),
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
        agent_names=("private",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    await record_silent_schedule_result_if_needed(
        entity_name="private",
        agent_names=("private",),
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


async def test_replayed_completed_silent_run_reenters_started_state(tmp_path: Path) -> None:
    """A replay must not leave the previous completion visible while its new attempt is running."""
    config = _config(agents={"watcher": AgentConfig(display_name="Watcher")})
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$replayed-run",
        prompt="Check for changes",
        agent_name="watcher",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )

    await record_silent_schedule_started_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    await record_silent_schedule_result_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason=None,
        response_text="A change was found",
    )
    [receipt_path] = list(
        (runtime_paths.storage_root / "agents" / "watcher" / "workspace" / ".mindroom" / "scheduled_runs").glob(
            "*.json",
        ),
    )
    completed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert completed_receipt["status"] == "completed"

    await record_silent_schedule_started_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )

    replayed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert replayed_receipt["status"] == "started"
    assert replayed_receipt["result"] is None
    assert replayed_receipt["response_text"] is None
    assert replayed_receipt["completed_at"] is None
    assert replayed_receipt["started_at"] == completed_receipt["started_at"]


async def test_replay_repairs_receipt_with_completion_before_start(tmp_path: Path) -> None:
    """A semantically invalid lifecycle must not poison timestamps on replay."""
    config = _config(agents={"watcher": AgentConfig(display_name="Watcher")})
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$invalid-lifecycle",
        prompt="Check for changes",
        agent_name="watcher",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )
    await record_silent_schedule_started_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    await record_silent_schedule_result_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason=None,
        response_text="A change was found",
    )
    [receipt_path] = list(
        (runtime_paths.storage_root / "agents" / "watcher" / "workspace" / ".mindroom" / "scheduled_runs").glob(
            "*.json",
        ),
    )
    malformed_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    malformed_receipt["started_at"] = "9999-12-31T23:59:59Z"
    malformed_receipt["completed_at"] = "2000-01-01T00:00:00Z"
    receipt_path.write_text(json.dumps(malformed_receipt), encoding="utf-8")

    await record_silent_schedule_started_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    await record_silent_schedule_result_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason=None,
        response_text="A newer change was found",
    )

    repaired_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert repaired_receipt["started_at"] <= repaired_receipt["completed_at"]


async def test_completion_repairs_started_receipt_from_future(tmp_path: Path) -> None:
    """Completion must not preserve a start timestamp later than itself."""
    config = _config(agents={"watcher": AgentConfig(display_name="Watcher")})
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$future-start",
        prompt="Check for changes",
        agent_name="watcher",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )
    await record_silent_schedule_started_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
    [receipt_path] = list(
        (runtime_paths.storage_root / "agents" / "watcher" / "workspace" / ".mindroom" / "scheduled_runs").glob(
            "*.json",
        ),
    )
    future_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    future_receipt["started_at"] = "9999-12-31T23:59:59Z"
    receipt_path.write_text(json.dumps(future_receipt), encoding="utf-8")

    await record_silent_schedule_result_if_needed(
        entity_name="watcher",
        agent_names=("watcher",),
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        suppression_reason=None,
        response_text="A change was found",
    )

    repaired_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert repaired_receipt["started_at"] <= repaired_receipt["completed_at"]


async def test_started_receipt_write_finishes_before_cancellation_propagates(tmp_path: Path) -> None:
    """Cancellation cannot revoke a durable write after the run is admitted."""
    config = _config(agents={"watcher": AgentConfig(display_name="Watcher")})
    runtime_paths = test_runtime_paths(tmp_path)
    envelope = request_envelope(
        room_id="!room:localhost",
        reply_to_event_id="$cancelled-start",
        prompt="Check for changes",
        agent_name="watcher",
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )
    write_started = Event()
    allow_write = Event()
    write_finished = Event()
    atomic_write = scheduled_run_records._atomic_write_receipt

    def blocking_write(*args: object, **kwargs: object) -> None:
        write_started.set()
        allow_write.wait()
        try:
            atomic_write(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            write_finished.set()

    with patch.object(scheduled_run_records, "_atomic_write_receipt", side_effect=blocking_write):
        task = asyncio.create_task(
            record_silent_schedule_started_if_needed(
                entity_name="watcher",
                agent_names=("watcher",),
                envelope=envelope,
                config=config,
                runtime_paths=runtime_paths,
            ),
        )
        assert await asyncio.to_thread(write_started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        cancellation_propagated_before_write = task.done()
        allow_write.set()
        assert await asyncio.to_thread(write_finished.wait, 5)
        with pytest.raises(asyncio.CancelledError):
            await task

    assert not cancellation_propagated_before_write
    receipts = list(
        (runtime_paths.storage_root / "agents" / "watcher" / "workspace" / ".mindroom" / "scheduled_runs").glob(
            "*.json",
        ),
    )
    assert len(receipts) == 1
