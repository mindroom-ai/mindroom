"""Room participation configuration contracts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.participation import RoomParticipationConfig
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


def test_room_participation_rejects_unknown_settings() -> None:
    """A misspelled pause must fail config loading instead of silently using the default."""
    with pytest.raises(ValidationError, match="debounce_second"):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "room_participation": {"lobby": {"agent": "helper", "debounce_second": 10}},
            },
        )


def test_typesafe_requires_room_opt_in() -> None:
    """Ordinary adaptive rooms must not send conversation text to another provider."""
    assert RoomParticipationConfig(agent="helper").typesafe is None
    room = RoomParticipationConfig.model_validate({"agent": "helper", "typesafe": {"threshold": 0.9}})
    assert room.typesafe is not None
    assert room.typesafe.threshold == 0.9


@pytest.mark.parametrize(
    "settings",
    [
        {"threshold": -0.1},
        {"threshold": 1.1},
        {"threshold": float("nan")},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"timeout_seconds": float("inf")},
        {"threshhold": 0.9},
    ],
)
def test_typesafe_rejects_invalid_settings(settings: dict[str, object]) -> None:
    """Invalid thresholds, unbounded waits, and typos must fail config loading."""
    with pytest.raises(ValidationError):
        RoomParticipationConfig.model_validate({"agent": "helper", "typesafe": settings})
