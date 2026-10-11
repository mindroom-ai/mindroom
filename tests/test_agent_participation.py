"""Agent participation configuration contracts."""

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.participation import ParticipationConfig


@pytest.mark.parametrize("settings", [None, {}])
def test_agent_participation_opt_in(settings: dict[str, object] | None) -> None:
    """Participation belongs to one agent and is disabled when omitted or null."""
    config = Config.model_validate(
        {
            "agents": {
                "helper": {"display_name": "Helper", "participation": settings},
                "ordinary": {"display_name": "Ordinary"},
            },
        },
    )
    participation = config.agents["helper"].participation
    assert config.agents["ordinary"].participation is None
    if settings is None:
        assert participation is None
    else:
        assert participation is not None
        assert participation.debounce_seconds == 3.0


@pytest.mark.parametrize("settings", [None, {}, {"decline_reaction": "👍", "debounce_seconds": 0}])
def test_participation_survives_authored_config_round_trip(settings: dict[str, object] | None) -> None:
    """Saving config preserves enabled defaults, explicit disables, and custom settings."""
    config = Config.model_validate({"agents": {"helper": {"display_name": "Helper", "participation": settings}}})
    authored = config.authored_model_dump()
    assert authored["agents"]["helper"]["participation"] == settings
    assert Config.model_validate(authored).agents["helper"].participation == config.agents["helper"].participation


def test_retired_room_participation_is_rejected() -> None:
    """The removed room format must not be silently ignored."""
    with pytest.raises(ValidationError, match="room_participation"):
        Config.model_validate({"room_participation": {"lobby": {}}})


@pytest.mark.parametrize("pause", [-1, 31, float("inf"), float("nan")])
def test_agent_participation_rejects_invalid_pause(pause: float) -> None:
    """Agent pauses must stay finite and within the supported window."""
    with pytest.raises(ValidationError):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper", "participation": {"debounce_seconds": pause}}},
            },
        )


def test_agent_participation_rejects_unknown_settings() -> None:
    """A misspelled pause must fail config loading instead of silently using the default."""
    with pytest.raises(ValidationError, match="debounce_second"):
        Config.model_validate(
            {
                "agents": {"helper": {"display_name": "Helper", "participation": {"debounce_second": 10}}},
            },
        )


def test_typesafe_requires_agent_opt_in() -> None:
    """Ordinary adaptive agents must not send conversation text to another provider."""
    assert ParticipationConfig().judgment is None
    participation = ParticipationConfig.model_validate(
        {"judgment": {"provider": "typesafe", "threshold": 0.9}},
    )
    assert participation.judgment is not None
    assert participation.judgment.threshold == 0.9


@pytest.mark.parametrize("reaction", [None, "👍", "👀", "👍🏽"])
def test_decline_reaction_is_optional(reaction: str | None) -> None:
    """Agents can retain silence or select a reaction, including composite emoji."""
    assert ParticipationConfig().decline_reaction is None
    assert ParticipationConfig.model_validate({"decline_reaction": reaction}).decline_reaction == reaction


@pytest.mark.parametrize("reaction", ["", "   ", "\n", "x" * 65, 123])
def test_decline_reaction_rejects_invalid_keys(reaction: object) -> None:
    """Reaction keys must be nonblank, bounded strings."""
    with pytest.raises(ValidationError):
        ParticipationConfig.model_validate({"decline_reaction": reaction})


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
        ParticipationConfig.model_validate({"judgment": {"provider": "typesafe", **settings}})


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
            "agents": {"helper": {"display_name": "Helper", "participation": {"judgment": settings}}},
            "models": {"cheap": {"provider": "test", "id": "cheap-model"}},
        },
    )
    assert config.agents["helper"].participation.judgment.provider == settings["provider"]


def test_judgment_rejects_unknown_model_alias() -> None:
    """Misspelled judge models must fail configuration instead of silently falling back."""
    with pytest.raises(ValidationError, match="Unknown judgment model"):
        Config.model_validate(
            {
                "agents": {
                    "helper": {
                        "display_name": "Helper",
                        "participation": {"judgment": {"provider": "llm", "model": "missing"}},
                    },
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
        ParticipationConfig.model_validate({"judgment": settings})
