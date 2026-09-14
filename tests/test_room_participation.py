"""Room participation configuration contracts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.matrix.state import MatrixState
from tests.conftest import test_runtime_paths


def test_room_participation_opt_in_and_direct_lookup(tmp_path: Path) -> None:
    """Participation is absent by default and available by explicit room ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    assert Config().get_room_participation("!room:localhost", runtime_paths) is None
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper"}},
            "room_participation": {"!room:localhost": {"agent": "helper"}},
        },
    )
    participation = config.get_room_participation("!room:localhost", runtime_paths)
    assert participation.agent == "helper"
    assert participation.debounce_seconds == 3.0


@pytest.mark.parametrize("agent", ["missing", "router", "team"])
def test_room_participation_rejects_non_individual_agent(agent: str) -> None:
    """Room participation rejects missing agents, router, and teams."""
    with pytest.raises(ValidationError, match="participation"):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "teams": {"team": {"display_name": "Team", "role": "Help", "agents": ["helper"]}},
                "room_participation": {"!room:localhost": {"agent": agent}},
            },
        )


@pytest.mark.parametrize("pause", [-1, 31, float("inf"), float("nan")])
def test_room_participation_rejects_invalid_pause(pause: float) -> None:
    """Room pauses must stay finite and within the supported window."""
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "room_participation": {"!room:localhost": {"agent": "helper", "debounce_seconds": pause}},
            },
        )


@pytest.mark.parametrize("key", ["lobby", "#lobby:localhost"])
def test_room_participation_resolves_persisted_alias(tmp_path: Path, key: str) -> None:
    """Managed room keys and full aliases resolve to the same room."""
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState()
    state.add_room("lobby", "!room:localhost", "#lobby:localhost", "Lobby")
    state.save(runtime_paths)
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper"}},
            "room_participation": {key: {"agent": "helper"}},
        },
    )
    assert config.get_room_participation("!room:localhost", runtime_paths).agent == "helper"
