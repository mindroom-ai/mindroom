"""Which model each entity of a turn runs: explicit choices, thread overrides, and the team rule."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.room_model_overrides import set_room_model_override
from mindroom.thread_models import set_thread_model_override
from mindroom.turn_model import (
    ModelScope,
    TeamTurnModelSelection,
    inherited_model_source,
    resolve_team_turn_models,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_ROOM = "!room:example.org"
_THREAD = "$root"


def _config(tmp_path: Path) -> Config:
    models = {
        name: ModelConfig(provider="openai", id=f"{name}-id") for name in ("default", "m1", "m2", "mt", "mx", "my")
    }
    return bind_runtime_paths(
        Config(
            agents={"a": AgentConfig(display_name="A", model="m1"), "b": AgentConfig(display_name="B", model="m2")},
            teams={"crew": TeamConfig(display_name="Crew", role="Work", agents=["a", "b"], model="mt")},
            models=models,
        ),
        test_runtime_paths(tmp_path),
    )


def _override(config: Config, model_name: str, *entities: str) -> None:
    set_thread_model_override(
        runtime_paths_for(config),
        thread_id=_THREAD,
        model_name=model_name,
        entity_names=entities,
        config=config,
    )


def _team_models(config: Config, *, entity: str = "crew", explicit: str | None = None) -> tuple[str, dict[str, str]]:
    selection = resolve_team_turn_models(
        config,
        runtime_paths_for(config),
        ModelScope(entity=entity, room_id=_ROOM, thread_id=_THREAD, explicit=explicit),
        ["a", "b"],
        None,
    )
    assert isinstance(selection, TeamTurnModelSelection)
    return selection.team_model_name, selection.member_model_names


@pytest.mark.parametrize(
    ("overrides", "explicit", "expected"),
    [
        pytest.param((), None, ("mt", {"a": "m1", "b": "m2"}), id="authored"),
        pytest.param((("mx", ("crew",)),), None, ("mx", {"a": "mx", "b": "mx"}), id="team-override-governs-members"),
        pytest.param((("my", ("a",)),), None, ("mt", {"a": "my", "b": "m2"}), id="member-override-only"),
        pytest.param(
            (("my", ("a",)), ("mx", ("crew",))),
            None,
            ("mx", {"a": "mx", "b": "mx"}),
            id="team-override-beats-member-override",
        ),
        pytest.param((("mx", ("crew",)),), "my", ("my", {"a": "my", "b": "my"}), id="explicit-beats-overrides"),
    ],
)
def test_a_team_turn_freezes_the_team_and_member_models_by_one_rule(
    tmp_path: Path,
    overrides: tuple[tuple[str, tuple[str, ...]], ...],
    explicit: str | None,
    expected: tuple[str, dict[str, str]],
) -> None:
    """An explicit model or the team's own thread override governs every member; otherwise each resolves its own."""
    config = _config(tmp_path)
    for model_name, entities in overrides:
        _override(config, model_name, *entities)
    assert _team_models(config, explicit=explicit) == expected


def test_an_ad_hoc_team_runs_its_hosts_model_without_governing_members(tmp_path: Path) -> None:
    """A team an agent hosts runs that agent's model; the host's thread override reaches only the host as a member."""
    config = _config(tmp_path)
    _override(config, "mx", "a")
    assert _team_models(config, entity="a") == ("mx", {"a": "mx", "b": "m2"})


@pytest.mark.parametrize(
    ("room_override", "room_model", "expected"),
    [
        pytest.param(None, None, "agent", id="agent"),
        pytest.param(None, "mx", "room_config", id="room-config"),
        pytest.param("my", "mx", "room", id="room-override-beats-room-config"),
    ],
)
def test_inherited_model_source_names_what_chose_the_room_level_model(
    tmp_path: Path,
    room_override: str | None,
    room_model: str | None,
    expected: str,
) -> None:
    """The source follows the turn's precedence below a thread choice, which it ignores."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    if room_model is not None:
        config.room_models[_ROOM] = room_model
    if room_override is not None:
        set_room_model_override(paths, room_id=_ROOM, model_name=room_override)
    _override(config, "mt", "a")
    assert inherited_model_source(config, paths, "a", _ROOM) == expected
