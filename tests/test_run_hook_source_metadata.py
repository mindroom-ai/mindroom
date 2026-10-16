"""Stored runs record the trusted hook source of the message that started them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY
from mindroom.message_target import MessageTarget
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


async def _turn(tmp_path: Path, hook_source: str | None) -> ResponseTurnContext:
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    request = _plain_request(MessageTarget.resolve("!room:localhost", "$thread", "$event"))
    request = replace(request, response_envelope=replace(request.response_envelope, hook_source=hook_source))
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
