"""Post-admission lifecycle effects must survive receipt replay without blocking ingestion."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.event_journal import AdmissionFacts, IngestionBatchAdmission, RoomMembershipPosition
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from tests.conftest import start_call_manager
from tests.journal_membership_helpers import admit_room_membership
from tests.test_bot_ready_hook import (
    _CONSUMER_GENERATION,
    _STREAM_ID,
    _agent_bot,
    _complete_frame,
    _validated_reported_membership_admission,
)
from tests.tool_job_helpers import job_owner, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_new", [True, False])
@pytest.mark.parametrize("membership", ["leave", "ban", "invite"])
async def test_owned_departure_reconciles_app_state_on_receipt_replay(
    tmp_path: Path,
    receipt_new: bool,
    membership: str,
) -> None:
    """A committed departure must retry call and invite cleanup before settlement."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership=membership,
        previous_epoch=0,
    )
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(generation=_CONSUMER_GENERATION, stream_id=_STREAM_ID)
    await admit_room_membership(principal, room_id, "join")
    batch = IngestionBatchAdmission(_STREAM_ID, 2, (admission,))
    facts = (await principal.admit_ingestion_batch(batch)).record_facts[0]
    if not receipt_new:
        facts = (await principal.admit_ingestion_batch(batch)).record_facts[0]
    assert facts.receipt_new is receipt_new
    manager = await start_call_manager(bot)
    with (
        patch.object(
            bot._room_lifecycle,
            "forget_invited_room",
        ) as forget,
    ):
        await bot._after_ingestion_admission(admission, facts, None)
    manager.on_sync_room_membership.assert_awaited_once_with(joined_room_ids=set(), left_room_ids={room_id})
    forget.assert_called_once_with(room_id)


@pytest.mark.asyncio
async def test_a_departure_cancels_the_agents_jobs_in_that_room(tmp_path: Path) -> None:
    """Nobody can see or cancel an agent's jobs in a room it left, so leaving cancels those jobs, and only those."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(generation=_CONSUMER_GENERATION, stream_id=_STREAM_ID)
    await admit_room_membership(principal, room_id, "join")
    facts = (await principal.admit_ingestion_batch(IngestionBatchAdmission(_STREAM_ID, 2, (admission,)))).record_facts[
        0
    ]
    runtime = await tool_job_runtime(tmp_path / "jobs")
    pin_background_tool_jobs(Config(background_tool_jobs=BackgroundToolJobsConfig(enabled=True)), bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    owner = replace(job_owner(), agent_name=bot.agent_name, room_id=room_id)

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(runtime, "here", tool_name="run_tests", depth=0, adapter={}, owner=owner, operation=forever)
        elsewhere = replace(owner, room_id="!elsewhere:localhost")
        await start_job(
            runtime,
            "elsewhere",
            tool_name="run_tests",
            depth=0,
            adapter={},
            owner=elsewhere,
            operation=forever,
        )
        with patch.object(bot._room_lifecycle, "forget_invited_room"):
            await bot._after_ingestion_admission(admission, facts, None)
        await wait_for_status(runtime, "here", "cancelled")
        assert {job.job_id: job.status for job in runtime.retained_jobs()}["elsewhere"] == "running"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [RoomMembershipPosition("join", 1), RoomMembershipPosition("leave", 2)])
async def test_old_departure_receipt_cannot_clear_a_new_membership(
    tmp_path: Path,
    position: RoomMembershipPosition,
) -> None:
    """Post-commit effects must honor the current epoch and membership."""
    bot = _agent_bot(tmp_path)
    admission = _validated_reported_membership_admission(
        bot,
        "!rejoined:localhost",
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    principal = MagicMock()
    principal.ingestion_membership_position = AsyncMock(return_value=position)
    with (
        patch.object(bot, "journal_principal", return_value=principal),
        patch.object(
            bot._room_lifecycle,
            "forget_invited_room",
        ) as forget,
    ):
        await bot._after_ingestion_admission(admission, AdmissionFacts(False, False), None)
    forget.assert_not_called()


@pytest.mark.asyncio
async def test_completed_frame_clears_join_notice_fences_for_joined_rooms(tmp_path: Path) -> None:
    """A successful source frame restores later actionable decrypt-failure notices."""
    bot = _agent_bot(tmp_path)
    room_id = "!joined:localhost"
    await bot._room_lifecycle._add_join_decrypt_notice_fence(room_id)
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    client = MagicMock(spec=nio.AsyncClient)
    client.rooms = {room_id: nio.MatrixRoom(room_id, bot.matrix_id.full_id)}
    bot.client = client
    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        await _complete_frame(bot)
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)
    assert bot._sync_continuity_store.load().pending_join_decrypt_fences == frozenset()


@pytest.mark.asyncio
async def test_authoritative_join_requests_call_reconciliation_after_frame_publication(tmp_path: Path) -> None:
    """A state-only rejoin must discover calls after Nio publishes current room state."""
    bot = _agent_bot(tmp_path)
    room_id = "!rejoined:localhost"
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
    )
    principal = MagicMock()
    principal.ingestion_membership_position = AsyncMock(return_value=RoomMembershipPosition("join", 1))
    manager = await start_call_manager(bot)
    with (
        patch.object(bot, "journal_principal", return_value=principal),
        patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()),
    ):
        await bot._after_ingestion_admission(admission, AdmissionFacts(True, False), None)
        await _complete_frame(bot)
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    manager.reconcile_joined_rooms.assert_awaited_once_with(frozenset({room_id}))


@pytest.mark.asyncio
async def test_call_room_updates_are_preserved_while_reconciliation_is_running(tmp_path: Path) -> None:
    """A later frame must retain new room work while an earlier scoped pass awaits."""
    bot = _agent_bot(tmp_path)
    first_room_id = "!first-call:localhost"
    second_room_id = "!second-call:localhost"
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    reconciled_scopes: list[frozenset[str] | None] = []

    async def reconcile(room_ids: frozenset[str] | None = None) -> None:
        reconciled_scopes.append(room_ids)
        if len(reconciled_scopes) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    manager = await start_call_manager(bot)
    manager.reconcile_joined_rooms.side_effect = reconcile

    with patch.object(bot, "_run_sync_response_side_effects", new=AsyncMock()):
        try:
            await bot._post_join_room_setup(first_room_id)
            await bot._post_join_room_setup(first_room_id)
            await _complete_frame(bot)
            await asyncio.wait_for(first_started.wait(), timeout=1)

            await bot._post_join_room_setup(second_room_id)
            await _complete_frame(bot)
            await asyncio.wait_for(second_started.wait(), timeout=1)
        finally:
            release_first.set()
            assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert reconciled_scopes == [frozenset({first_room_id}), frozenset({second_room_id})]
