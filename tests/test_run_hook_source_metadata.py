"""Stored runs record the trusted hook source and the intent of the message that started them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import TurnIntent
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


class _BuiltRecorder(Exception):  # noqa: N818
    """Stops a turn as soon as its recorder is built."""


async def _turn(
    tmp_path: Path,
    hook_source: str | None,
    intent: TurnIntent = TurnIntent.USER_MESSAGE,
) -> ResponseTurnContext:
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    envelope = request.response_envelope
    envelope = replace(envelope, hook_source=hook_source, origin=replace(envelope.origin, intent=intent))
    request = replace(request, response_envelope=envelope)
    runtime = await runner.prepare_response_runtime(request)
    return runner._agent_turn_context(
        request,
        runtime=runtime,
        run_id="run",
        active_event_ids={"$event"},
        transient_enrichment_items=(),
        system_enrichment_items=(),
    )


@pytest.mark.asyncio
async def test_agent_turn_records_hook_source_in_run_metadata(tmp_path: Path) -> None:
    """An automation prompt's hook source travels into the run's stored metadata."""
    turn = await _turn(tmp_path, "automation/exploration")
    assert turn.matrix_run_metadata is not None
    assert turn.matrix_run_metadata[HOOK_SOURCE_RUN_METADATA_KEY] == "automation/exploration"


@pytest.mark.asyncio
async def test_agent_turn_without_hook_source_records_none(tmp_path: Path) -> None:
    """A person's message adds no hook source key."""
    turn = await _turn(tmp_path, None)
    assert HOOK_SOURCE_RUN_METADATA_KEY not in (turn.matrix_run_metadata or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", [TurnIntent.USER_MESSAGE, TurnIntent.JOB_FOLLOW_UP])
async def test_agent_turn_records_its_intent_in_run_metadata(tmp_path: Path, intent: TurnIntent) -> None:
    """A stored job follow-up says so, since its prompt is MindRoom's instruction rather than its requester's words."""
    turn = await _turn(tmp_path, None, intent)
    assert turn.matrix_run_metadata is not None
    assert turn.matrix_run_metadata[TURN_INTENT_RUN_METADATA_KEY] == intent


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_process_and_respond", "_process_and_respond_streaming"])
async def test_a_turn_stopped_before_its_run_starts_keeps_its_origin(tmp_path: Path, method: str) -> None:
    """The turn recorder starts with the turn's origin, so a turn interrupted while it prepares is stored with it."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    envelope = request.response_envelope
    envelope = replace(
        envelope,
        hook_source="automation/exploration",
        origin=replace(envelope.origin, intent=TurnIntent.JOB_FOLLOW_UP),
    )
    request = replace(request, response_envelope=envelope)

    with (
        patch.object(runner, "_build_turn_recorder", side_effect=_BuiltRecorder) as build,
        pytest.raises(_BuiltRecorder),
    ):
        await getattr(runner, method)(request)

    metadata = build.call_args.kwargs["matrix_run_metadata"]
    assert metadata[TURN_INTENT_RUN_METADATA_KEY] == TurnIntent.JOB_FOLLOW_UP
    assert metadata[HOOK_SOURCE_RUN_METADATA_KEY] == "automation/exploration"
