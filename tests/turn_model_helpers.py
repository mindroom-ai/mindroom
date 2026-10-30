"""Build the turn model a test passes to ``create_agent``, and the member models a test's team turn runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.turn_model import ModelScope, TurnModel, resolve_team_turn_models, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def turn_model(config: Config, runtime_paths: RuntimePaths, entity: str, model_name: str | None = None) -> TurnModel:
    """Return the model ``entity``'s turn runs: ``model_name`` when given, else its authored model."""
    return resolve_turn_model(config, runtime_paths, ModelScope(entity=entity, explicit=model_name), None)


def team_member_models(
    config: Config,
    runtime_paths: RuntimePaths,
    team: str | None,
    member_names: Sequence[str],
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, str]:
    """Return the members' models a turn of ``team`` in the room and thread runs, as the response runner freezes them."""
    scope = ModelScope(entity=team, room_id=room_id, thread_id=thread_id)
    return resolve_team_turn_models(config, runtime_paths, scope, member_names, None).member_model_names
