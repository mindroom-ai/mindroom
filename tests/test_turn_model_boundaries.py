"""Edges of turn model choice that one model owner must keep or knowingly change: budgets, precedence, teams."""

# Pinned elsewhere, so not repeated here:
# - budget_model: tests/test_budget_monitor.py::test_without_a_monitor_only_zero_caps_apply (no monitor, zero cap swaps),
#   ::test_over_budget_requester_gets_fallback_for_priced_models_only (requester None keeps the model, with a monitor),
#   ::test_fallback_model_stays_usable_past_the_cap (with a monitor), ::test_bridge_bot_requesters_are_never_budgeted,
#   ::test_an_unpriced_alias_of_a_priced_model_is_swapped_too.
# - resolve_runtime_model model names: tests/test_thread_models.py::test_resolve_runtime_model_prefers_thread_override,
#   ::test_resolve_runtime_model_thread_override_beats_room_override, ::test_resolve_runtime_model_active_model_beats_thread_override,
#   ::test_resolve_runtime_model_ignores_stale_thread_override, ::test_resolve_runtime_model_without_thread_id_keeps_authored_model,
#   ::test_resolve_runtime_model_requires_runtime_paths_for_thread_id; tests/test_room_models.py::test_runtime_room_override_precedence,
#   ::test_stale_runtime_room_override_falls_back_to_static_room_model.
# - Team rule: tests/test_turn_model_selection.py::test_a_team_turn_freezes_the_team_and_member_models_by_one_rule;
#   materialized members follow room then member thread overrides:
#   tests/test_room_models.py::test_runtime_model_precedence_applies_to_materialized_team_members.
# - Team budgets: tests/test_response_runner_team.py::TestAgentBot::test_over_budget_requester_team_uses_fallback_for_priced_models,
#   tests/test_budget_enforcement.py::test_openai_compat_team_completion_budgets_priced_models[False|True].
# - Cold cache reads `overridden` (persisted room override naming the agent's own model skips the judge):
#   tests/test_response_runner_focused.py::test_cold_cache_skipped_for_override. The order "cold-cache judgment, then budget"
#   (ResponseRunner._cold_cache_model before prepare_response_runtime's _budgeted_model) is pinned by no test; the nearest are
#   tests/test_response_runner_focused.py::test_cold_cache_model_used_for_reply and tests/test_budget_enforcement.py::test_scheduled_model_is_budgeted_too.

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

import pytest

from mindroom.api import openai_compat
from mindroom.budgets.monitor import budget_model
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.budgets import BudgetsConfig
from mindroom.config.main import Config, ResolvedRuntimeModel
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.room_model_overrides import set_room_model_override
from mindroom.teams.members import TeamTurnModelSelection, materialize_exact_team_members, resolve_team_turn_models
from mindroom.thread_models import set_thread_model_override
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_ROOM = "!room:example.org"
_THREAD = "$root"
_ALICE = "@alice:example.org"
_WINDOWS = {"m1": 1_000, "m2": 2_000, "m3": 3_000, "m4": 4_000, "m5": 5_000}


def _config(tmp_path: Path, *, team_model: str | None = "mt") -> Config:
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
            teams={"crew": TeamConfig(display_name="Crew", role="Work", agents=["a", "b"], model=team_model)},
            room_models={"lobby": "m2"},
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
    monkeypatch: pytest.MonkeyPatch,
    layers: _Layers,
    expected: tuple[str, int | None, bool],
) -> None:
    """Explicit beats thread beats persisted room beats authored room_models beats the agent; only the first three count as overrides."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    monkeypatch.setattr("mindroom.matrix.state.get_room_alias_from_id", lambda *_args: "lobby")
    if layers.room is not None:
        set_room_model_override(paths, room_id=_ROOM, model_name=layers.room)
    if layers.thread is not None:
        _thread_override(config, layers.thread, "a")
    resolved = config.resolve_runtime_model(
        entity_name="a",
        active_model_name=layers.explicit,
        active_context_window=layers.window,
        room_id=layers.room_id,
        thread_id=_THREAD,
        runtime_paths=paths,
    )
    assert (resolved.model_name, resolved.context_window, resolved.overridden) == expected


def test_unknown_entity_raises_without_a_room_but_runs_default_in_one(tmp_path: Path) -> None:
    """Without a room an unknown entity is an error; with a room it silently gets ``default``."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    with pytest.raises(ValueError, match=r"^Unknown entity: ghost\. Available entities: a, b, crew, router$"):
        config.resolve_runtime_model(entity_name="ghost")
    assert config.resolve_runtime_model(
        entity_name="ghost",
        room_id=_ROOM,
        runtime_paths=paths,
    ) == ResolvedRuntimeModel(model_name="default", context_window=None, overridden=False)


def test_a_team_without_a_model_raises_unless_a_persisted_room_override_names_one(tmp_path: Path) -> None:
    """``TeamConfig(model=None)`` raises with or without a room; only a persisted room override avoids it."""
    config = _config(tmp_path, team_model=None)
    paths = runtime_paths_for(config)
    for room_id in (None, _ROOM):
        with pytest.raises(ValueError, match=r"^Team crew has no model configured$"):
            config.resolve_runtime_model(entity_name="crew", room_id=room_id, runtime_paths=paths)
    set_room_model_override(paths, room_id=_ROOM, model_name="m3")
    resolved = config.resolve_runtime_model(entity_name="crew", room_id=_ROOM, runtime_paths=paths)
    assert (resolved.model_name, resolved.overridden) == ("m3", True)


def test_exact_members_without_a_model_map_ignore_the_teams_thread_override(tmp_path: Path) -> None:
    """Built with no model map, members resolve their own models although the team rule hands them the team's override."""
    config = _config(tmp_path)
    paths = runtime_paths_for(config)
    _thread_override(config, "mx", "crew")
    members = materialize_exact_team_members(
        ["a", "b"],
        config=config,
        runtime_paths=paths,
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="crew",
            requester_id=_ALICE,
            room_id=_ROOM,
            thread_id=_THREAD,
            resolved_thread_id=_THREAD,
            session_id=f"{_ROOM}:{_THREAD}",
        ),
        refresh_scheduler=None,
        active_model_names=None,
    )
    assert members.model_names == {"a": "m1", "b": "m2"}
    assert [build.spec.model.identity.id for build in members.builds] == ["m1-id", "m2-id"]
    team_rule = resolve_team_turn_models("crew", ["a", "b"], _ROOM, config, paths, thread_id=_THREAD)
    assert team_rule.member_model_names == {"a": "mx", "b": "mx"}


def test_openai_team_models_budget_each_authored_member_and_default_a_modelless_team(tmp_path: Path) -> None:
    """The OpenAI-compatible path budgets ``team.model or "default"`` and each member's authored model separately."""
    config = _config(tmp_path, team_model=None)
    seen: list[str] = []

    def budget(model_name: str) -> str:
        seen.append(model_name)
        return f"budgeted-{model_name}"

    assert openai_compat._openai_team_models("crew", config, budget) == TeamTurnModelSelection(
        team_model_name="budgeted-default",
        member_model_names={"a": "budgeted-m1", "b": "budgeted-m2"},
    )
    assert seen == ["default", "m1", "m2"]
    real_budget = partial(budget_model, config, runtime_paths_for(config), None, _ALICE)
    assert openai_compat._openai_team_models("crew", config, real_budget) == TeamTurnModelSelection(
        team_model_name="default",
        member_model_names={"a": "fb", "b": "m2"},
    )
