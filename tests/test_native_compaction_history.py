"""Native checkpoints participate in history planning without replacing stored runs."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.session.summary import SessionSummary
from agno.team import Team

from mindroom.config.agent import TeamConfig
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.native import restore_native_history
from mindroom.history.runtime import (
    ScopeSessionContext,
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.storage import write_scope_state
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.native_compaction import record_native_checkpoint
from mindroom.openai_models import MindRoomOpenAIResponses
from tests.conftest import seed_session
from tests.history_helpers import _agent, _completed_run, _completed_team_run, _make_config, _session, _team_session

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("change", ["none", "model", "endpoint", "summary", "disabled", "missing", "invalid_threshold"])
def test_restore_native_policy_requires_latest_compatible_response(change: str) -> None:
    """A rebuild must not recover stale policy from an older checkpoint or foreign route."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    checkpoint = ModelResponse(content="Ready")
    record_native_checkpoint(
        checkpoint,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "small-checkpoint"}],
        model.native_compaction,
    )
    latest = ModelResponse(content="Waiting for approval")
    record_native_checkpoint(latest, [], None if change == "disabled" else model.native_compaction)
    if change == "missing":
        latest.provider_data = None
    elif change == "invalid_threshold":
        latest.provider_data["mindroom_native_compaction"]["threshold"] = True
    messages = [
        Message(role="user", content="Canonical facts"),
        Message(role="assistant", content=checkpoint.content, provider_data=checkpoint.provider_data),
        Message(role="user", content="Continue"),
        Message(role="assistant", content=latest.content, provider_data=latest.provider_data),
    ]
    run = _completed_run("paused", messages=messages)
    session = _session("session", runs=[run])
    rebuilt = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    rebuilt.configure_native_compaction(threshold=2048)
    if change == "model":
        rebuilt.id = "unsupported-route"
    elif change == "endpoint":
        rebuilt.base_url = "https://other.example/v1"
    elif change == "summary":
        session.summary = SessionSummary(summary="New portable summary")
    restore_native_history(rebuilt, persisted_run=run, session=session)
    replay = rebuilt._format_messages(messages)
    if change == "none":
        assert rebuilt.native_compaction is not None
        assert rebuilt.native_compaction.threshold == 1024
        assert replay[0]["type"] == "compaction"
    else:
        assert rebuilt.native_compaction is None
        assert replay[0] == {"role": "user", "content": "Canonical facts"}


@pytest.mark.asyncio
async def test_native_budget_keeps_large_canonical_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Counting canonical history here would compact it before the checkpoint reaches the API."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=120000)
    response = ModelResponse(content="Ready")
    record_native_checkpoint(
        response,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "small-checkpoint"}],
        model.native_compaction,
    )
    session = _session(
        "session",
        runs=[
            _completed_run(
                "old",
                messages=[
                    Message(role="user", content="large old transcript " * 50000),
                    Message(role="assistant", content="Ready", provider_data=response.provider_data),
                ],
            ),
        ],
    )
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    text_compact = AsyncMock(side_effect=AssertionError("Canonical history must remain intact"))
    monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    prepared = await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, db, session),
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, config=config)
    assert final.replay_plan is not None
    assert final.replay_plan.add_history_to_context
    assert final.replay_plan.num_history_runs is None
    assert final.replay_plan.num_history_messages is None
    assert final.replay_plan.estimated_tokens < 1000
    assert final.replays_persisted_history
    assert len(session.runs or []) == 1
    assert len(session.runs[0].messages[0].content) > 900000
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "manual", "bounded", "custom_summary", "disabled", "scheduled"])
async def test_native_activation_respects_history_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Manual, bounded, custom-model, disabled, and scheduled policies keep portable semantics."""
    compaction = CompactionConfig(threshold_tokens=120000, enabled=case != "disabled")
    if case == "custom_summary":
        compaction.model = "summary"
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=compaction,
        num_history_runs=2 if case == "bounded" else None,
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000),
            "summary": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000),
        },
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    session = _session("session", runs=[_completed_run("old")])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    if case == "manual":
        write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    if case == "manual":
        # The policy must reach the existing text owner, with native replay disabled.
        async def text_compact(**_kwargs: object) -> None:
            assert model.native_compaction is None
            message = "Reached portable compaction"
            raise RuntimeError(message)

        monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    kwargs = dict(  # noqa: C408
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, db, session),
        allow_native_compaction=case != "scheduled",
    )
    if case == "manual":
        with pytest.raises(RuntimeError, match="Reached portable compaction"):
            await prepare_scope_history(**kwargs)
    else:
        await prepare_scope_history(**kwargs)
        assert (model.native_compaction is not None) is (case == "normal")
    db.close()


@pytest.mark.asyncio
async def test_text_summary_change_invalidates_checkpoint(tmp_path: Path) -> None:
    """A retained native checkpoint cannot hide a newer portable summary rewrite."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=120000)
    old_route = model.native_compaction.route
    session = _session("session", summary=SessionSummary(summary="New portable summary."))
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(HistoryScope(kind="agent", scope_id="test_agent"), db, session),
    )
    assert model.native_compaction is not None
    assert model.native_compaction.route != old_route
    db.close()


@pytest.mark.asyncio
async def test_team_native_activation_uses_team_model(tmp_path: Path) -> None:
    """A team's native policy belongs to its leader model, not its first member."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    config.teams["team"] = TeamConfig(agents=["test_agent"], display_name="Team", role="Coordinate work.")
    member = _agent()
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    team = Team(id="team", model=model, members=[member])
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    session = _team_session("session", team_id="team", runs=[_completed_team_run("run", team_id="team")])
    seed_session(db, session)
    await prepare_bound_scope_history(
        agents=[member],
        team=team,
        team_name="team",
        full_prompt="Continue.",
        config=config,
        runtime_paths=paths,
        scope_context=ScopeSessionContext(HistoryScope(kind="team", scope_id="team"), db, session),
        active_model_name="default",
        active_context_window=200000,
        static_prompt_tokens=100,
    )
    assert model.native_compaction is not None
    assert model.native_compaction.threshold == 120000
    db.close()
