"""Stored runs record the trusted hook source and the intent of the message that started them, and where it was sent."""

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

    from mindroom.response_runner import ResponseRequest
    from mindroom.response_turn import ResponseTurnContext

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


class _BuiltRecorder(Exception):  # noqa: N818
    """Stops a turn as soon as its recorder is built."""


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
        transient_enrichment_items=(),
        system_enrichment_items=(),
    )


@pytest.mark.asyncio
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
@pytest.mark.parametrize("method", ["_process_and_respond", "_process_and_respond_streaming"])
async def test_a_turn_stopped_before_its_run_starts_keeps_its_origin(tmp_path: Path, method: str) -> None:
    """The turn recorder starts with the turn's origin, so a turn interrupted while it prepares is stored with it."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _request("automation/exploration", TurnIntent.JOB_FOLLOW_UP)

    with (
        patch.object(runner, "_build_turn_recorder", side_effect=_BuiltRecorder) as build,
        pytest.raises(_BuiltRecorder),
    ):
        await getattr(runner, method)(request)

    metadata = build.call_args.kwargs["matrix_run_metadata"]
    assert metadata[TURN_INTENT_RUN_METADATA_KEY] == TurnIntent.JOB_FOLLOW_UP
    assert metadata[HOOK_SOURCE_RUN_METADATA_KEY] == "automation/exploration"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_process_and_respond", "_process_and_respond_streaming"])
async def test_a_turn_stopped_before_its_run_starts_keeps_its_room(tmp_path: Path, method: str) -> None:
    """The turn recorder starts with the turn's room and thread, so a turn that fails while it prepares names them."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    build_recorder = runner._build_turn_recorder
    recorders = []

    def build_then_stop(**kwargs: object) -> None:
        recorders.append(build_recorder(**kwargs))
        raise _BuiltRecorder

    with (
        patch.object(runner, "_build_turn_recorder", side_effect=build_then_stop),
        pytest.raises(_BuiltRecorder),
    ):
        await getattr(runner, method)(_request(None, TurnIntent.USER_MESSAGE))

    metadata = recorders[0].run_metadata
    assert (metadata["room_id"], metadata["thread_id"]) == ("!room:localhost", "$thread")
