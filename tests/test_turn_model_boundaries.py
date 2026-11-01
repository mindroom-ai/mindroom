"""Edges of turn model choice: budgets, precedence, the context window, unknown entities, and teams."""

# Pinned elsewhere, so not repeated here:
# - budget_model: tests/test_budget_monitor.py::test_without_a_monitor_only_zero_caps_apply (no monitor, zero cap swaps),
#   ::test_over_budget_requester_gets_fallback_for_priced_models_only (requester None keeps the model, with a monitor),
#   ::test_fallback_model_stays_usable_past_the_cap (with a monitor), ::test_bridge_bot_requesters_are_never_budgeted,
#   ::test_an_unpriced_alias_of_a_priced_model_is_swapped_too.
# - Model names by layer: tests/test_thread_models.py (thread overrides) and tests/test_room_models.py (room choices).
# - Team rule: tests/test_turn_model_selection.py::test_a_team_turn_freezes_the_team_and_member_models_by_one_rule;
#   materialized members follow room then member thread overrides:
#   tests/test_room_models.py::test_runtime_model_precedence_applies_to_materialized_team_members.
# - Team budgets: tests/test_response_runner_team.py::TestAgentBot::test_over_budget_requester_team_uses_fallback_for_priced_models,
#   tests/test_budget_enforcement.py::test_openai_compat_team_completion_budgets_priced_models[False|True].
# - Cold cache reads `overridden` (persisted room override naming the agent's own model skips the judge):
#   tests/test_response_runner_focused.py::test_cold_cache_skipped_for_override. The order "cold-cache judgment, then budget"
#   (ResponseRunner._cold_cache_model before prepare_response_runtime's budget) is pinned by no test; the nearest are
#   tests/test_response_runner_focused.py::test_cold_cache_model_used_for_reply and tests/test_budget_enforcement.py::test_scheduled_model_is_budgeted_too.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.budgets.monitor import budget_model
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.room_model_overrides import set_room_model_override
from mindroom.thread_models import set_thread_model_override
from mindroom.turn_model import (
    ModelScope,
    TeamTurnModelSelection,
    TurnBudget,
    TurnModel,
    resolve_team_turn_models,
    resolve_turn_model,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_ROOM = "!room:example.org"
_THREAD = "$root"
_ALICE = "@alice:example.org"
_WINDOWS = {"m1": 1_000, "m2": 2_000, "m3": 3_000, "m4": 4_000, "m5": 5_000}


def _config(tmp_path: Path) -> Config:
    models = {
        name: ModelConfig(provider="openai", id=f"{name}-id", context_window=_WINDOWS.get(name))
        for name in ("default", *_WINDOWS, "mt", "mx")
    }
    models["m1"] = ModelConfig(
        provider="openai",
        id="m1-id",
        context_window=1_000,
        pricing=ModelPricing(input=5, output=30),
    )
    models["fb"] = ModelConfig(provider="openai", id="fb-id", pricing=ModelPricing(input=0.2, output=1))
    return bind_runtime_paths(
        Config(
            agents={"a": AgentConfig(display_name="A", model="m1"), "b": AgentConfig(display_name="B", model="m2")},
            teams={"crew": TeamConfig(display_name="Crew", role="Work", agents=["a", "b"], model="mt")},
            room_models={_ROOM: "m2"},
            models=models,
            budgets=BudgetsConfig(fallback_model="fb", monthly_limit_usd=0),
        ),
        test_runtime_paths(tmp_path),
    )


def _thread_override(config: Config, model_name: str, *entities: str) -> None:
    set_thread_model_override(
        runtime_paths_for(config),
        thread_id=_THREAD,
        model_name=model_name,
        entity_names=entities,
        config=config,
    )


def test_budget_without_a_monitor_swaps_only_a_known_requesters_priced_non_fallback_model(tmp_path: Path) -> None:
    """With no monitor a zero cap swaps a priced model, but never for an anonymous requester or the fallback itself."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    assert budget_model(config, paths, None, _ALICE, "m1") == "fb"
    assert budget_model(config, paths, None, None, "m1") == "m1"
    assert budget_model(config, paths, None, _ALICE, "fb") == "fb"


@dataclass(frozen=True)
class _Layers:
    room_id: str | None = None
    room: str | None = None
    thread: str | None = None
    explicit: str | None = None
    window: int | None = None


@pytest.mark.parametrize(
    ("layers", "expected"),
    [
        pytest.param(_Layers(), ("m1", 1_000, False), id="authored"),
        pytest.param(_Layers(room_id=_ROOM), ("m2", 2_000, False), id="authored-room-models-not-overridden"),
        pytest.param(_Layers(room_id=_ROOM, room="m3"), ("m3", 3_000, True), id="persisted-room"),
        pytest.param(_Layers(room_id=_ROOM, room="m1"), ("m1", 1_000, True), id="persisted-room-naming-authored-model"),
        pytest.param(_Layers(room_id=_ROOM, room="m3", thread="m4"), ("m4", 4_000, True), id="thread-beats-room"),
        pytest.param(_Layers(thread="m4"), ("m4", 4_000, True), id="thread-without-room"),
        pytest.param(
            _Layers(room_id=_ROOM, room="m3", thread="m4", explicit="m5"),
            ("m5", 5_000, True),
            id="explicit-beats-all",
        ),
        pytest.param(_Layers(explicit="gone"), ("gone", None, True), id="explicit-unknown-model-unchecked"),
        pytest.param(_Layers(explicit="m5", window=77), ("m5", 77, True), id="explicit-window-echoed"),
        pytest.param(_Layers(window=77), ("m1", 77, False), id="authored-window-echoed"),
    ],
)
def test_one_agent_resolves_model_window_and_overridden_flag_by_layer(
    tmp_path: Path,
    layers: _Layers,
    expected: tuple[str, int | None, bool],
) -> None:
    """Explicit beats thread beats persisted room beats authored room_models beats the agent; only the first three count as overrides."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    if layers.room is not None:
        set_room_model_override(paths, room_id=_ROOM, model_name=layers.room)
    if layers.thread is not None:
        _thread_override(config, layers.thread, "a")
    resolved = resolve_turn_model(
        config,
        paths,
        ModelScope(
            entity="a",
            room_id=layers.room_id,
            thread_id=_THREAD,
            explicit=layers.explicit,
            explicit_context_window=layers.window,
        ),
        None,
    )
    assert (resolved.model_name, resolved.context_window, resolved.overridden) == expected


@pytest.mark.parametrize("room_id", [None, _ROOM])
def test_an_unknown_entity_never_runs_a_model(tmp_path: Path, room_id: str | None) -> None:
    """An entity the config does not name raises, with a room or without one."""
    config = _config(tmp_path)
    with pytest.raises(ValueError, match=r"^Unknown entity: ghost\. Available entities: a, b, crew, router$"):
        resolve_turn_model(config, runtime_paths_for(config), ModelScope(entity="ghost", room_id=room_id), None)


def test_a_team_must_name_a_model() -> None:
    """A team without a model is rejected when the config loads, not at its first reply."""
    with pytest.raises(ValidationError):
        TeamConfig(display_name="Crew", role="Work", agents=["a"], model=None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "reference"),
    [
        pytest.param({"agents": {"a": {"display_name": "A", "model": "gone"}}}, "agents.a.model: 'gone'", id="agent"),
        pytest.param(
            {"rooms": {"lobby": {}}, "room_models": {"lobby": "gone"}},
            "room_models.lobby: 'gone'",
            id="room-models",
        ),
    ],
)
def test_a_model_choice_naming_no_configured_model_fails_the_config_load(
    tmp_path: Path,
    raw: dict[str, object],
    reference: str,
) -> None:
    """An entity or room model that names no configured model is rejected at load, not at the reply that runs it."""
    paths = test_runtime_paths(tmp_path)
    with pytest.raises(ConfigRuntimeValidationError, match=re.escape(f"Unknown model for {reference}")):
        Config.validate_with_runtime({"models": {"default": {"provider": "openai", "id": "d"}}, **raw}, paths)


def test_the_budget_swaps_the_model_a_turn_runs_and_keeps_the_one_it_asked_for(tmp_path: Path) -> None:
    """An over-budget requester runs the fallback; the turn keeps its requested model for the next budget check."""
    config = _config(tmp_path)
    budget = TurnBudget(monitor=None, requester_id=_ALICE)
    assert resolve_turn_model(config, runtime_paths_for(config), ModelScope(entity="a"), budget) == TurnModel(
        model_name="fb",
        requested_model_name="m1",
        context_window=None,
        overridden=False,
    )


def test_an_openai_team_turn_budgets_the_team_and_each_member_separately(tmp_path: Path) -> None:
    """Without a room or thread the team and each member run their authored models under the requester's budget."""
    config = _config(tmp_path)
    assert resolve_team_turn_models(
        config,
        runtime_paths_for(config),
        ModelScope(entity="crew"),
        ["a", "b"],
        TurnBudget(monitor=None, requester_id=_ALICE),
    ) == TeamTurnModelSelection(team_model_name="mt", member_model_names={"a": "fb", "b": "m2"})
