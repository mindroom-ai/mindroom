"""The production team turn reproduces the team prompt goldens.

Each golden is replayed through ``team_response_stream`` with the recording provider: the members get their prompt
and the leader's task alone, the leader gets the roster prompt and the delegation tool, then both member results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.messages import Message
from tests.agno_parity.golden_files import render
from tests.agno_parity.prompt_scenarios import golden_path, load_golden, record_scenario, regenerated_golden

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = ["team_coordinate", "team_collaborate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_the_production_team_turn_sends_every_golden_request(name: str, tmp_path: Path) -> None:
    """Every request of the team turn equals the golden's byte for byte, through the owned provider layer."""
    run = await record_scenario(name, tmp_path)

    regenerated = regenerated_golden(name, run, tmp_path)
    assert regenerated == load_golden(name)
    assert render(regenerated) == golden_path(name).read_text(encoding="utf-8")
    assert run.calls
    assert all(isinstance(message, Message) for call in run.calls for message in call.messages)
    # The leader's prompt is built once per turn and sent with both of its requests.
    leader_requests = [
        call for call in run.calls if any(tool.name.startswith("delegate_task_to_") for tool in call.tools)
    ]
    assert len(leader_requests) == 2
    assert len({request.messages[0].content for request in leader_requests}) == 1
