"""Run the team prompt-golden scenarios on the production path and record every request they send.

``team_response_stream`` builds the members and the leader through the production builders and runs them on the
owned runtime; the recorder at the provider seam keeps every request, so the leader's prompt, the delegation tool,
and the members' requests are compared with the goldens as the production path sends them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.agno_parity.prompt_scenarios import SCENARIOS, ModelCall, Recorder

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class TeamScenarioRun:
    """What one team scenario sent: its recorded input and every model call, in build order."""

    input: dict[str, Any]
    calls: list[ModelCall]


async def run_team_scenario(name: str, root: Path) -> TeamScenarioRun:
    """Run one team scenario under ``root`` against the recording provider."""
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)
    with recorder.installed():
        output = await scenario.run(root)
    return TeamScenarioRun(output.input, recorder.ordered_calls())
