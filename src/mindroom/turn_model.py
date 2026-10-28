"""The model one turn runs: an explicit choice, a saved thread or room choice, or the authored model, then the budget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.budgets.monitor import budget_model
from mindroom.entity_resolution import resolve_room_scoped_model_override
from mindroom.room_model_overrides import resolve_room_model_override
from mindroom.thread_models import resolve_thread_model_override

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_AD_HOC_MODEL_NAME = "default"


@dataclass(frozen=True)
class ModelScope:
    """Whose turn it is and where, plus any model chosen for it before resolution.

    ``entity`` names the agent, configured team, or router that owns the turn's model; a team an agent hosts names
    that agent. None is an ad hoc history scope with no owning entity, which runs the default model.
    ``explicit`` is a model already chosen for this turn: a scheduled model, a continuation's model, or a switch.
    ``explicit_context_window`` keeps a window a caller already planned the turn's history with.
    """

    entity: str | None
    room_id: str | None = None
    thread_id: str | None = None
    explicit: str | None = None
    explicit_context_window: int | None = None


@dataclass(frozen=True)
class TurnBudget:
    """The requester whose monthly budget a turn spends; without a monitor, configured caps apply against zero spend."""

    monitor: BudgetMonitor | None
    requester_id: str | None
    log_fallback: bool = True


@dataclass(frozen=True)
class TurnModel:
    """The model a turn runs and the one it asked for before the budget, with that model's context window."""

    model_name: str
    requested_model_name: str
    context_window: int | None
    # An explicit choice, a thread override, or a saved room override chose the requested model.
    overridden: bool


@dataclass(frozen=True)
class TeamTurnModelSelection:
    """Model aliases frozen together for one team turn."""

    team_model_name: str
    member_model_names: dict[str, str]


def _requested_model(config: Config, runtime_paths: RuntimePaths, scope: ModelScope) -> tuple[str, bool]:
    if scope.explicit is not None:
        return scope.explicit, True
    if scope.entity is None:
        return _AD_HOC_MODEL_NAME, False
    # The authored model also checks that the entity exists, so an unknown name never runs any model.
    authored = config.resolve_entity(scope.entity).model_name
    if scope.thread_id is not None:
        thread_model = resolve_thread_model_override(runtime_paths, scope.thread_id, config=config).active.get(
            scope.entity,
        )
        if thread_model is not None:
            return thread_model, True
    if scope.room_id is None:
        return authored, False
    room_override = resolve_room_model_override(runtime_paths, scope.room_id, configured_models=config.models).active
    if room_override is not None:
        return room_override, True
    room_model = resolve_room_scoped_model_override(config.room_models, scope.room_id, runtime_paths)
    return room_model or authored, False


def _budgeted(config: Config, runtime_paths: RuntimePaths, model_name: str, budget: TurnBudget | None) -> str:
    if budget is None:
        return model_name
    return budget_model(
        config,
        runtime_paths,
        budget.monitor,
        budget.requester_id,
        model_name,
        log_fallback=budget.log_fallback,
    )


def resolve_turn_model(
    config: Config,
    runtime_paths: RuntimePaths,
    scope: ModelScope,
    budget: TurnBudget | None,
) -> TurnModel:
    """Return the model one entity's turn runs.

    Precedence: an explicit choice, the thread's saved choice for the entity, the room's saved choice, the authored
    `room_models` entry, then the entity's authored model; the requester's budget may then swap in its fallback.
    ``budget`` None skips the budget, for a turn that runs a model already budgeted or no requester's turn.
    """
    requested, overridden = _requested_model(config, runtime_paths, scope)
    model_name = _budgeted(config, runtime_paths, requested, budget)
    context_window = (
        scope.explicit_context_window
        if scope.explicit_context_window is not None
        else config.get_model_context_window(model_name)
    )
    return TurnModel(
        model_name=model_name,
        requested_model_name=requested,
        context_window=context_window,
        overridden=overridden,
    )


def resolve_team_turn_models(
    config: Config,
    runtime_paths: RuntimePaths,
    scope: ModelScope,
    member_names: Sequence[str],
    budget: TurnBudget | None,
) -> TeamTurnModelSelection:
    """Freeze the models of one team turn: its owner's and each member's, each under the requester's budget.

    An explicit choice, or a configured team's own thread choice, governs every member, because a configured team's
    access reaches its members. Otherwise the team resolves its own model and each member resolves its own.
    """
    explicit = scope.explicit
    if explicit is None and scope.entity in config.teams and scope.thread_id is not None:
        explicit = resolve_thread_model_override(runtime_paths, scope.thread_id, config=config).active.get(scope.entity)
    if explicit is not None:
        team_requested = explicit
        member_requested = dict.fromkeys(member_names, explicit)
    else:
        team_requested, _ = _requested_model(config, runtime_paths, scope)
        member_requested = {
            member: _requested_model(
                config,
                runtime_paths,
                ModelScope(entity=member, room_id=scope.room_id, thread_id=scope.thread_id),
            )[0]
            for member in member_names
        }
    budgeted = {
        name: _budgeted(config, runtime_paths, name, budget) for name in {team_requested, *member_requested.values()}
    }
    return TeamTurnModelSelection(
        team_model_name=budgeted[team_requested],
        member_model_names={member: budgeted[name] for member, name in member_requested.items()},
    )
