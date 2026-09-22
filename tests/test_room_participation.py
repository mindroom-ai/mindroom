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
            "room_participation": {"!room:localhost": {}},
        },
    )
    participation = config.get_room_participation("!room:localhost", runtime_paths)
    assert participation is not None
    assert participation.debounce_seconds == 3.0


@pytest.mark.parametrize("pause", [-1, 31, float("inf"), float("nan")])
def test_room_participation_rejects_invalid_pause(pause: float) -> None:
    """Room pauses must stay finite and within the supported window."""
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "room_participation": {"!room:localhost": {"debounce_seconds": pause}},
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
            "room_participation": {key: {}},
        },
    )
    assert config.get_room_participation("!room:localhost", runtime_paths) is not None


def test_room_participation_rejects_unknown_settings() -> None:
    """A misspelled pause must fail config loading instead of silently using the default."""
    with pytest.raises(ValidationError, match="debounce_second"):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "room_participation": {"lobby": {"debounce_second": 10}},
            },
        )


def test_typesafe_requires_room_opt_in() -> None:
    """Ordinary adaptive rooms must not send conversation text to another provider."""
    assert RoomParticipationConfig().judgment is None
    room = RoomParticipationConfig.model_validate(
        {"judgment": {"provider": "typesafe", "threshold": 0.9}},
    )
    assert room.judgment is not None
    assert room.judgment.threshold == 0.9


@pytest.mark.parametrize("reaction", [None, "👍", "👀", "👍🏽"])
def test_decline_reaction_is_optional(reaction: str | None) -> None:
    """Rooms can retain silence or select a reaction, including composite emoji."""
    assert RoomParticipationConfig().decline_reaction is None
    assert RoomParticipationConfig.model_validate({"decline_reaction": reaction}).decline_reaction == reaction


@pytest.mark.parametrize("reaction", ["", "   ", "\n", "x" * 65, 123])
def test_decline_reaction_rejects_invalid_keys(reaction: object) -> None:
    """Reaction keys must be nonblank, bounded strings."""
    with pytest.raises(ValidationError):
        RoomParticipationConfig.model_validate({"decline_reaction": reaction})


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
        RoomParticipationConfig.model_validate({"judgment": {"provider": "typesafe", **settings}})


@pytest.mark.parametrize(
    "settings",
    [
        {"provider": "llm", "model": "cheap"},
        {"provider": "typesafe", "threshold": 0.9},
    ],
)
def test_judgment_backend_is_explicit_and_model_alias_is_validated(settings: dict[str, object]) -> None:
    """A dedicated decision backend is separate from the responding agent's model."""
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper"}},
            "models": {"cheap": {"provider": "test", "id": "cheap-model"}},
            "room_participation": {"lobby": {"judgment": settings}},
        },
    )
    assert config.room_participation["lobby"].judgment.provider == settings["provider"]


def test_judgment_rejects_unknown_model_alias() -> None:
    """Misspelled judge models must fail configuration instead of silently falling back."""
    with pytest.raises(ValidationError, match="Unknown judgment model"):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper"}},
                "room_participation": {
                    "lobby": {"judgment": {"provider": "llm", "model": "missing"}},
                },
            },
        )


@pytest.mark.parametrize(
    "settings",
    [
        {"provider": "unknown"},
        {"provider": "llm"},
        {"provider": "llm", "model": ""},
        {"provider": "llm", "model": "cheap", "threshold": 0.8},
        {"provider": "llm", "model": "cheap", "timeout_seconds": 0},
        {"provider": "llm", "model": "cheap", "timeout_seconds": float("nan")},
        {"provider": "typesafe", "model": "cheap"},
    ],
)
def test_judgment_rejects_mixed_backend_settings(settings: dict[str, object]) -> None:
    """A provider switch must not silently ignore settings meant for the old backend."""
    with pytest.raises(ValidationError):
        RoomParticipationConfig.model_validate({"judgment": settings})
