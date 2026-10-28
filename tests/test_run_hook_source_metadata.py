"""Stored runs record the trusted hook source and the intent of the message that started them, and where it was sent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import TurnIntent
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.response_runner import ResponseRequest
    from mindroom.response_turn import ResponseTurnContext


def _request(hook_source: str | None, intent: TurnIntent) -> ResponseRequest:
    request = _plain_request(MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    envelope = request.response_envelope
    envelope = replace(envelope, hook_source=hook_source, origin=replace(envelope.origin, intent=intent))
    return replace(request, response_envelope=envelope)


async def _turn(tmp_path: Path, hook_source: str | None, intent: TurnIntent) -> ResponseTurnContext:
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _request(hook_source, intent)
    runtime = await runner.prepare_response_runtime(request)
    return runner._agent_turn_context(
        request,
        runtime=runtime,
        run_id="run",
        active_event_ids={"$event"},
        knowledge=None,
        transient_enrichment_items=(),
        compaction_lifecycle=None,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(
    ("hook_source", "intent"),
    [("automation/exploration", TurnIntent.USER_MESSAGE), (None, TurnIntent.JOB_FOLLOW_UP)],
)
async def test_agent_turn_records_its_origin_in_run_metadata(
    tmp_path: Path,
    hook_source: str | None,
    intent: TurnIntent,
) -> None:
    """A stored run says which automation asked, if any, and whether MindRoom wrote its prompt, as a job follow-up."""
    metadata = (await _turn(tmp_path, hook_source, intent)).matrix_run_metadata or {}
    assert metadata.get(HOOK_SOURCE_RUN_METADATA_KEY) == hook_source
    assert metadata[TURN_INTENT_RUN_METADATA_KEY] == intent


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_a_turn_stopped_before_its_run_starts_keeps_its_origin_and_room(
    tmp_path: Path,
    *,
    streaming: bool,
) -> None:
    """A turn stopped before its run starts records the turn's origin, room, and thread for its replay."""
    bot = _bot(tmp_path)
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    persist = MagicMock()

    with (
        patch("mindroom.ai._prepare_agent_run_context", new=AsyncMock(side_effect=asyncio.CancelledError)),
        patch("mindroom.response_turn.persist_interrupted_replay_snapshot", new=persist),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=streaming),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        await runner.generate_response(_request("automation/exploration", TurnIntent.JOB_FOLLOW_UP))

    persist.assert_called_once()
    metadata = persist.call_args.kwargs["snapshot"].run_metadata
    assert metadata[TURN_INTENT_RUN_METADATA_KEY] == TurnIntent.JOB_FOLLOW_UP
    assert metadata[HOOK_SOURCE_RUN_METADATA_KEY] == "automation/exploration"
    assert (metadata["room_id"], metadata["thread_id"]) == ("!room:localhost", "$thread")
