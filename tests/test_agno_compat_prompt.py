"""Private prompt preparation must leave live instruction state intact on failure."""

from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.team import Team

from mindroom.history.prompt_tokens import estimate_agent_static_tokens, estimate_team_static_tokens
from tests.conftest import FakeModel

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize("is_team", [False, True])
def test_prompt_builder_failure_restores_original_instruction_list(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_team: bool,
) -> None:
    """A failed static-token estimate must not change the next real request's instructions."""
    entity: Agent | Team
    estimate: Callable
    if is_team:
        entity = Team(members=[], model=FakeModel(id="test"), telemetry=False)
        estimate = estimate_team_static_tokens
    else:
        entity = Agent(model=FakeModel(id="test"), telemetry=False)
        estimate = estimate_agent_static_tokens
    original = ["Retain this exact live instruction list"]
    entity._tool_instructions = original

    def fail(**_kwargs: object) -> None:
        message = "prompt construction failed"
        raise RuntimeError(message)

    monkeypatch.setattr(entity, "get_system_message", fail)
    with pytest.raises(RuntimeError, match="prompt construction failed"):
        estimate(entity, "Current prompt")
    assert entity._tool_instructions is original
