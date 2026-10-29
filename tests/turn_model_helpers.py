"""Build the turn model a test passes to ``create_agent``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.turn_model import ModelScope, TurnModel, resolve_turn_model

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def turn_model(config: Config, runtime_paths: RuntimePaths, entity: str, model_name: str | None = None) -> TurnModel:
    """Return the model ``entity``'s turn runs: ``model_name`` when given, else its authored model."""
    return resolve_turn_model(config, runtime_paths, ModelScope(entity=entity, explicit=model_name), None)
