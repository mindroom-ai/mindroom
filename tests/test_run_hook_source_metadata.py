"""Stored runs record the trusted hook source and the intent of the message that started them, and where it was sent."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, TURN_INTENT_RUN_METADATA_KEY
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import TurnIntent
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.reply_turn.request import ResponseRequest
    from mindroom.response_turn import ResponseTurnContext


def _request(hook_source: str | None, intent: TurnIntent) -> ResponseRequest:
    request = _plain_request(MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    envelope = request.response_envelope
    envelope = replace(envelope, hook_source=hook_source, origin=replace(envelope.origin, intent=intent))
    return replace(request, response_envelope=envelope)


async def _turn(tmp_path: Path, hook_source: str | None, intent: TurnIntent) -> ResponseTurnContext:
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _request(hook_source, intent)
    runtime = await runner.prepare_response_runtime(request, show_tool_calls=True)
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
@pytest.mark.parametrize(
    ("hook_source", "intent"),
    [("automation/exploration", TurnIntent.USER_MESSAGE), (None, TurnIntent.JOB_FOLLOW_UP)],
)
async def test_team_turn_records_its_origin_in_run_metadata(
    tmp_path: Path,
    hook_source: str | None,
    intent: TurnIntent,
) -> None:
    """A team's stored run says which automation asked and whether MindRoom wrote its prompt, as an agent's does."""
    bot = _bot(tmp_path)
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    seen: list[ResponseTurnContext] = []

    def team_stream(_spec: object, ctx: ResponseTurnContext, **_kwargs: object) -> AsyncIterator[str]:
        seen.append(ctx)

        async def answer() -> AsyncIterator[str]:
            yield "Team answer."

        return answer()

    with patch_response_runner_module(
        team_response_stream=team_stream,
        should_use_streaming=AsyncMock(return_value=False),
        typing_indicator=_noop_typing,
        apply_post_response_effects=AsyncMock(),
    ):
        await runner.generate_team_response_helper(
            _request(hook_source, intent),
            member_names=[bot.agent_name],
            team_mode="coordinate",
        )

    [ctx] = seen
    metadata = ctx.matrix_run_metadata or {}
    assert metadata.get(HOOK_SOURCE_RUN_METADATA_KEY) == hook_source
    assert metadata[TURN_INTENT_RUN_METADATA_KEY] == intent
