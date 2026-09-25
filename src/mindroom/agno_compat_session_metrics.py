"""Count only new usage when Agno saves an already-accounted run."""

from __future__ import annotations

import threading
import weakref
from copy import deepcopy
from dataclasses import dataclass, field, fields
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from agno.agent import _storage as agent_storage
from agno.metrics import BaseMetrics, RunMetrics, SessionMetrics
from agno.run.team import TeamRunOutput
from agno.team import _session as team_session
from agno.team import _storage as team_storage

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from agno.team import Team

    type _Session = AgentSession | TeamSession
    type _Run = RunOutput | TeamRunOutput

# AGNO_COMPAT: Saving a resumed run adds already-counted metrics again.
# Reason: Agent and Team add the full cumulative run metrics at every pause,
# checkpoint and completion; the previous run has already been replaced in session.runs.
# They also omit totals when a pre-created session has no session_data mapping.
# Upstream issue: No separate issue identified; the only tracking was the PR below.
# Upstream PR: https://github.com/agno-agi/agno/pull/10353, closed without merge; no replacement identified.
# Remove when: Agno counts each run's usage once across pause/resume, checkpoints
# and team-member saves, initializes totals for bare sessions, and preserves
# totals for history removed by the owner.
# Coverage: tests/test_agno_compat_session_metrics.py; tests/test_usage_storage.py.
_SUPPORTED_VERSION = "3.0.9"
_ORIGINAL_AGENT_UPDATE = agent_storage.update_session_metrics
_ORIGINAL_TEAM_UPDATE = team_session.update_session_metrics
_LOCK = threading.Lock()
_DATABASES: weakref.WeakSet[BaseDb] = weakref.WeakSet()
_SNAPSHOT_ATTRIBUTE = "_mindroom_accounted_usage"
_COUNTERS = tuple(item.name for item in fields(BaseMetrics))


@dataclass
class _AccountedUsage:
    """Private runtime state, excluded from Agno's dataclass serialization."""

    runs: dict[str, RunMetrics] = field(default_factory=dict)


def register_database(database: BaseDb) -> None:
    """Install the pinned repair and restrict it to storage owned by MindRoom."""
    with _LOCK:
        installed = (
            agent_storage.update_session_metrics is _update_agent_metrics
            and team_session.update_session_metrics is _update_team_metrics
        )
        if not installed:
            if (
                version("agno") != _SUPPORTED_VERSION
                or agent_storage.update_session_metrics is not _ORIGINAL_AGENT_UPDATE
                or team_session.update_session_metrics is not _ORIGINAL_TEAM_UPDATE
            ):
                msg = "Unsupported Agno session metrics implementation"
                raise RuntimeError(msg)
            agent_storage.update_session_metrics = cast("Any", _update_agent_metrics)
            team_session.update_session_metrics = cast("Any", _update_team_metrics)
        _DATABASES.add(database)


def seed_accounted_usage(session: _Session) -> None:
    """Freeze loaded contributions before continuation or checkpoint code can mutate them."""
    accounted = _AccountedUsage()
    if session.session_data and session.session_data.get("session_metrics") is not None:
        accounted.runs = {
            run.run_id: deepcopy(run.metrics)
            for run in _with_members(session.runs or ())
            if run.run_id is not None and run.metrics is not None
        }
    session.__dict__[_SNAPSHOT_ATTRIBUTE] = accounted


def _with_members(runs: Iterable[_Run]) -> Iterator[_Run]:
    for run in runs:
        yield run
        if isinstance(run, TeamRunOutput):
            yield from _with_members(run.member_responses)


def _update_agent_metrics(agent: Agent, session: AgentSession, run_response: RunOutput) -> None:
    if agent.db not in _DATABASES:
        _ORIGINAL_AGENT_UPDATE(agent, session, run_response)
        return
    _accumulate_new_usage(
        session,
        agent_storage.get_session_metrics_internal(agent, session),
        (run_response,),
    )


def _update_team_metrics(team: Team, session: TeamSession, run_response: TeamRunOutput) -> None:
    if team.db not in _DATABASES:
        _ORIGINAL_TEAM_UPDATE(team, session, run_response)
        return
    _accumulate_new_usage(
        session,
        team_storage.get_session_metrics_internal(team, session),
        _with_members((run_response,)),
    )


def _accumulate_new_usage(session: _Session, totals: SessionMetrics, runs: Iterable[_Run]) -> None:
    if session.session_data is None:
        session.session_data = {}
    accounted = session.__dict__.setdefault(_SNAPSHOT_ATTRIBUTE, _AccountedUsage())
    assert isinstance(accounted, _AccountedUsage)
    updates: dict[str, RunMetrics] = {}
    for run in runs:
        if run.metrics is None or (run.run_id is not None and run.run_id in updates):
            continue
        previous = accounted.runs.get(run.run_id) if run.run_id is not None else None
        if previous is not None:
            totals.accumulate_from_run(_negated_metrics(previous))
        totals.accumulate_from_run(run.metrics)
        if run.run_id is not None:
            updates[run.run_id] = deepcopy(run.metrics)
    session.session_data["session_metrics"] = totals.to_dict()
    accounted.runs.update(updates)


def _negated_metrics(metrics: RunMetrics) -> RunMetrics:
    """Undo one prior contribution using Agno's own scalar and model merge semantics."""
    payload = metrics.to_dict()
    _negate_counters(payload)
    for entries in payload.get("details", {}).values():
        for entry in entries:
            _negate_counters(entry)
    return RunMetrics.from_dict(payload)


def _negate_counters(payload: dict[str, Any]) -> None:
    for key in _COUNTERS:
        if payload.get(key) is not None:
            payload[key] = -payload[key]
    for key in ("additional_metrics", "provider_metrics"):
        if isinstance(values := payload.get(key), dict):
            payload[key] = {
                name: -value if isinstance(value, (int, float)) else value for name, value in values.items()
            }
