"""The production team turn reproduces the team prompt goldens.

Each golden is replayed through ``team_response_stream`` with the recording provider: the members get their prompt
and the leader's task alone, the leader gets the roster prompt and the delegation tool, then both member results.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.llm.messages import Message
from tests.agno_parity.prompt_scenarios import SCENARIOS, call_dict, load_golden, normalize
from tests.providers.golden_support import ordered_json
from tests.team_runtime.parity_support import run_team_scenario

if TYPE_CHECKING:
    from pathlib import Path

GOLDENS = ["team_coordinate", "team_collaborate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GOLDENS)
async def test_the_production_team_turn_sends_every_golden_request(name: str, tmp_path: Path) -> None:
    """Every request of the team turn equals the golden's byte for byte, through the owned provider layer."""
    golden = load_golden(name)

    run = await run_team_scenario(name, tmp_path)

    assert SCENARIOS[name].description == golden["description"]
    assert normalize(run.input, tmp_path) == golden["input"]
    assert run.calls
    assert all(isinstance(message, Message) for call in run.calls for message in call.messages)
    requests = normalize([call_dict(call) for call in run.calls], tmp_path)
    assert requests == golden["requests"]
    assert ordered_json(requests) == ordered_json(golden["requests"])
    # The leader's prompt is built once per turn and sent with both of its requests.
    leader_requests = [
        call
        for call in run.calls
        if any(tool["function"]["name"].startswith("delegate_task_to_") for tool in call.tools or ())
    ]
    assert len(leader_requests) == 2
    assert len({request.messages[0].content for request in leader_requests}) == 1
