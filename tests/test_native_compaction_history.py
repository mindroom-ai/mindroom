"""Native checkpoints participate in history planning without replacing stored runs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import httpx
import pytest

from mindroom.config.agent import TeamConfig
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, ModelConfig
from mindroom.history.native import configure_native_history, restore_native_history, with_native_history
from mindroom.history.replay import compaction_marker, compaction_summary_message
from mindroom.history.request_messages import scope_history_messages
from mindroom.history.runtime import (
    _ScopeCompactionLifecycleResult,
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import _with_scope_state
from mindroom.history.types import HistoryScope, HistoryScopeState, ScopeSummary
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import checkpoint_summary, record_native_checkpoint, replay_basis
from mindroom.providers.registry import build_model_provider
from mindroom.session_storage.agent_storage import create_state_storage
from tests.conftest import seed_session
from tests.fakes.summary_model import summary_model
from tests.history_helpers import (
    _completed_run,
    _completed_team_run,
    _make_config,
    _session,
    _team_session,
    compact_runs,
)
from tests.providers.anthropic_family_support import isolated_provider_environment, provider_for
from tests.providers.bare_adapter import bare_adapter
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.history.runtime import HistoryPreparationInputs
    from mindroom.history.types import NativeHistory
    from mindroom.llm.provider import ModelProvider, NativeCompactionReplay
    from mindroom.llm.runs import RunRecord
    from mindroom.providers.openai.responses import ResponsesAdapter

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_CHECKPOINT = [{"type": "compaction", "id": "cmp", "encrypted_content": "small-checkpoint"}]


@pytest.fixture(autouse=True)
def _no_provider_environment() -> Iterator[None]:
    with isolated_provider_environment():
        yield


def _inputs(config: Config) -> HistoryPreparationInputs:
    """Resolve the configured test agent's history inputs for a 100-token static prompt."""
    return resolve_agent_preparation_inputs(
        agent_name="test_agent",
        history_settings=config.resolve_entity("test_agent").history_settings,
        config=config,
        static_prompt_tokens=100,
    )


def _sent(adapter: ResponsesAdapter, messages: Sequence[Message], native: NativeHistory | None) -> dict[str, Any]:
    """Return the ``responses.create`` arguments a request of ``messages`` sends under ``native``."""
    return adapter._request_kwargs(with_native_history(ModelRequest(messages=tuple(messages)), native))


def _continues(sent: dict[str, Any]) -> str | None:
    """Return the stored response a request continues, if any."""
    return sent.get("previous_response_id")


@pytest.mark.asyncio
async def test_bounded_legacy_replay_after_sqlite_restart_preserves_stored_history(tmp_path: Path) -> None:
    """Budget fitting must use safe explicit tool input without rewriting durable legacy records."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(enabled=False, reserve_tokens=1000, replay_window_tokens=1000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=5000)},
    )
    legacy = [
        Message(role="user", content="Check the service."),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "fc_status",
                    "call_id": "call_status",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ],
            provider_data={
                "response_id": "resp_legacy",
                "reasoning_output": {"type": "reasoning", "id": "rs_last", "summary": []},
            },
        ),
        Message(role="tool", tool_call_id="fc_status", content="ready"),
    ]
    db = create_state_storage("test_agent", tmp_path)
    seed_session(
        db,
        _session(
            "session",
            runs=[
                _completed_run("old", messages=[Message(role="user", content="0123456789" * 1800)]),
                _completed_run("recent", messages=legacy),
            ],
        ),
    )
    db = create_state_storage("test_agent", tmp_path)
    session = db.load_session("session", "agent")
    assert session is not None
    original = [run.to_dict() for run in session.runs]
    model = responses_adapter(tmp_path, store=True)
    resolved = _inputs(config)

    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=resolved,
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
    )
    final = finalize_history_preparation(
        prepared_scope_history=prepared,
        config=config,
        static_prompt_tokens=100,
    )

    assert final.replay_plan is not None
    assert final.replay_plan.mode == "limited"
    assert final.replay_plan.num_history_runs == 1
    messages = scope_history_messages(
        session.runs,
        scope=_SCOPE,
        num_history_runs=final.replay_plan.num_history_runs,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    assert model.estimate_portable_replay_tokens(messages) <= final.replay_plan.estimated_tokens <= 1000
    messages.append(Message(role="user", content="Continue"))
    sent = _sent(model, messages, final.native_history)
    assert _continues(sent) is None
    assert sent["input"] == [
        {"role": "user", "content": "Check the service."},
        {
            "type": "function_call",
            "call_id": "call_status",
            "name": "get_status",
            "arguments": "{}",
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "call_status", "output": "ready"},
        {"role": "user", "content": "Continue"},
    ]
    persisted = db.load_session("session", "agent")
    assert persisted is not None
    assert [run.to_dict() for run in persisted.runs] == original
    assert [run.to_dict() for run in session.runs] == original


def test_portable_replay_follows_each_plans_replay_budget(tmp_path: Path) -> None:
    """An unbounded plan continues the stored response; a bounded one replays its history explicitly."""
    config, _ = _make_config(tmp_path)
    model = responses_adapter(tmp_path, store=True)
    resolved = _inputs(config)
    messages = [
        Message(role="assistant", content="Previous answer", provider_data={"response_id": "resp_previous"}),
        Message(role="user", content="Continue"),
    ]
    for budget in (1000, None, 2000):
        native = configure_native_history(
            model,
            plan=replace(resolved.execution_plan, hard_replay_budget_tokens=budget),
            history_settings=resolved.history_settings,
            session=None,
            allowed=False,
        )
        sent = _sent(model, messages, native)
        if budget is None:
            assert _continues(sent) == "resp_previous"
            assert sent["input"] == [{"role": "user", "content": "Continue"}]
        else:
            assert _continues(sent) is None
            assert sent["input"][0] == {"role": "assistant", "content": "Previous answer"}


@pytest.mark.parametrize("change", ["none", "model", "endpoint", "summary", "disabled", "missing", "invalid_threshold"])
def test_restore_native_policy_requires_latest_compatible_response(tmp_path: Path, change: str) -> None:
    """A rebuild must not recover stale policy from an older checkpoint or foreign route."""
    paused = responses_adapter(tmp_path, store=False)
    native = paused.native_compaction_settings(threshold=1024)
    assert native is not None
    checkpoint = Message(role="assistant", content="Ready")
    record_native_checkpoint(checkpoint, _CHECKPOINT, native)
    latest = Message(role="assistant", content="Waiting for approval")
    record_native_checkpoint(latest, [], None if change == "disabled" else native)
    if change == "missing":
        latest.provider_data = None
    elif change == "invalid_threshold":
        assert latest.provider_data is not None
        latest.provider_data["mindroom_native_compaction"]["threshold"] = True
    messages = [
        Message(role="user", content="Canonical facts"),
        checkpoint,
        Message(role="user", content="Continue"),
        latest,
    ]
    run = _completed_run("paused", messages=messages)
    session = _session("session", runs=[run])
    rebuilt_kwargs: dict[str, Any] = {"store": False}
    if change == "endpoint":
        rebuilt_kwargs["base_url"] = "https://other.example/v1"
    rebuilt = responses_adapter(tmp_path, "unsupported-route" if change == "model" else "gpt-6-astra", **rebuilt_kwargs)
    if change == "summary":
        session.summary = ScopeSummary(text="New portable summary", updated_at=datetime.now(UTC))

    restored = restore_native_history(rebuilt, persisted_run=run, session=session)

    assert restored is not None
    replay = _sent(rebuilt, messages, restored)["input"]
    if change == "none":
        assert restored.native_compaction is not None
        assert restored.native_compaction.threshold == 1024
        assert replay[0]["type"] == "compaction"
    else:
        assert restored.native_compaction is None
        assert replay[0] == {"role": "user", "content": "Canonical facts"}


@pytest.mark.asyncio
async def test_native_budget_keeps_large_canonical_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sizing by canonical history or the checkpoint's billed size would compact before the API sees the checkpoint."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = responses_adapter(tmp_path, store=False)
    answer = Message(
        role="assistant",
        content="Ready",
        # Billed for the transcript the checkpoint replaced, over the 183,516-token hard budget.
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=190000)),
    )
    record_native_checkpoint(answer, _CHECKPOINT, model.native_compaction_settings(threshold=120000))
    run = _completed_run("old", messages=[Message(role="user", content="large old transcript " * 50000), answer])
    run.model = model.identity.id
    run.model_provider = model.identity.provider
    session = _session("session", runs=[run])
    db = create_state_storage("test_agent", tmp_path)
    seed_session(db, session)
    text_compact = AsyncMock(side_effect=AssertionError("Canonical history must remain intact"))
    monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)
    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
    )
    final = finalize_history_preparation(
        prepared_scope_history=prepared,
        config=config,
        static_prompt_tokens=100,
    )
    assert final.replay_plan is not None
    assert final.replay_plan.add_history_to_context
    assert final.replay_plan.num_history_runs is None
    assert final.replay_plan.num_history_messages is None
    assert final.replay_plan.estimated_tokens < 1000
    assert final.replays_persisted_history
    assert len(session.runs) == 1
    assert len(str(session.runs[0].messages[0].content)) > 900000


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_compact", [False, True])
async def test_proxy_replay_budget_counts_tokens_and_discards_stored_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_compact: bool,
) -> None:
    """Dense text and hidden stored context must not bypass portable replay fitting."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(enabled=auto_compact, reserve_tokens=1000, replay_window_tokens=6000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=30000)},
    )
    monkeypatch.setattr(
        "mindroom.history.compaction.build_model_provider",
        lambda *_args: summary_model(model_id="summary"),
    )
    monkeypatch.setattr(
        "mindroom.history.compaction.generate_compaction_summary",
        AsyncMock(return_value="Keep the recent instruction"),
    )
    model = responses_adapter(tmp_path, base_url="https://proxy.example/v1", store=True)
    latest = [
        Message(role="user", content="Keep the recent instruction"),
        Message(
            role="assistant",
            content="Ready",
            provider_data={"response_id": "resp_large_context"},
            metrics=MessageMetrics(tokens=TokenCounts(input_tokens=9000, output_tokens=10)),
        ),
    ]
    session = _session(
        "session",
        runs=[
            _completed_run("old", messages=[Message(role="user", content="0123456789" * 1800)]),
            _completed_run("recent", messages=latest),
        ],
    )
    db = create_state_storage("test_agent", tmp_path)
    seed_session(db, session)
    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
    )
    final = finalize_history_preparation(
        prepared_scope_history=prepared,
        config=config,
        static_prompt_tokens=100,
    )
    assert final.replay_plan is not None
    if auto_compact:
        assert prepared.compaction_reply_outcome == "success"
        assert session.summary is not None
        assert session.summary.text == "Keep the recent instruction"
        assert session.runs == []
    else:
        assert final.replay_plan.mode == "limited"
        assert final.replay_plan.num_history_runs == 1
        assert final.replay_plan.estimated_tokens < 100
        assert len(session.runs) == 2
    assert final.native_history is not None
    assert final.native_history.native_compaction is None
    sent = _sent(model, [*latest, Message(role="user", content="Continue")], final.native_history)
    assert _continues(sent) is None
    assert sent["input"][0] == {"role": "user", "content": "Keep the recent instruction"}
    assert latest[-1].provider_data == {"response_id": "resp_large_context"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "manual", "bounded", "custom_summary", "disabled", "scheduled"])
@pytest.mark.parametrize("authored_claude", [False, True])
async def test_native_activation_respects_history_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    *,
    authored_claude: bool,
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
    model = (
        provider_for(
            {
                "provider": "anthropic",
                "id": "claude-sonnet-5",
                "extra_kwargs": {
                    "api_key": "test-key",
                    "context_management": {"edits": [{"type": "compact_20260112"}]},
                },
            },
            tmp_path / "claude",
            httpx.AsyncClient(),
        )
        if authored_claude
        else responses_adapter(tmp_path, store=False)
    )
    session = _session("session", runs=[_completed_run("old")])
    if case == "manual":
        session.metadata = _with_scope_state(
            session.metadata,
            _SCOPE,
            HistoryScopeState(force_compact_before_next_run=True),
        )
    db = create_state_storage("test_agent", tmp_path)
    seed_session(db, session)
    # A manual request must reach the portable text owner, which returns no outcome here.
    text_compact = AsyncMock(return_value=_ScopeCompactionLifecycleResult(outcome=None, reply_outcome="failed"))
    monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)

    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
        allow_native_compaction=case != "scheduled",
    )

    native = prepared.native_history
    assert (native is not None and native.native_compaction is not None) is (case == "normal")
    assert text_compact.await_count == (1 if case == "manual" else 0)


@pytest.mark.asyncio
async def test_text_summary_change_invalidates_checkpoint(tmp_path: Path) -> None:
    """A retained native checkpoint cannot hide a newer portable summary rewrite."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = responses_adapter(tmp_path, store=False)
    old = model.native_compaction_settings(threshold=120000)
    assert old is not None
    session = _session("session", summary=ScopeSummary(text="New portable summary.", updated_at=datetime.now(UTC)))
    db = create_state_storage("test_agent", tmp_path)
    seed_session(db, session)
    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=_inputs(config),
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
    )
    assert prepared.native_history is not None
    assert prepared.native_history.native_compaction is not None
    assert prepared.native_history.native_compaction.route != old.route


def test_native_requests_record_the_history_they_replay(tmp_path: Path) -> None:
    """A request replaying from a checkpoint that skips runs records an empty basis; unknown history counts as covering.

    A run that checkpointed before waiting for approval sorts after the turns it waited for, which replay then skips.
    """
    config, _ = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = responses_adapter(tmp_path, store=False)
    resolved = _inputs(config)

    def configured(runs: list[RunRecord]) -> NativeCompactionReplay:
        native = configure_native_history(
            model,
            plan=resolved.execution_plan,
            history_settings=resolved.history_settings,
            session=None,
            allowed=True,
            runs=runs,
        )
        assert native is not None
        assert native.native_compaction is not None
        return native.native_compaction

    first, waited = _completed_run("first"), _completed_run("waited")
    settings = configured([])

    def resumed(basis: str | None, *, route: str = settings.route, run_id: str = "resumed") -> RunRecord:
        answer = Message(role="assistant", content="Done")
        items = [{"type": "compaction", "encrypted_content": "checkpoint"}]
        record_native_checkpoint(answer, items, replace(settings, route=route, basis=basis))
        return _completed_run(run_id, messages=[Message(role="user", content="Go"), answer])

    covering = resumed(replay_basis(None, [first, waited]))
    assert configured([first, waited, covering]).basis == replay_basis(None, [first, waited, covering])
    skipping = resumed(replay_basis(None, [first]))
    assert configured([first, waited, skipping]).basis == ""
    # A checkpoint recorded before bases existed counts as covering, as native replay assumes.
    unknown = resumed(None)
    assert configured([first, waited, unknown]).basis == replay_basis(None, [first, waited, unknown])
    # Replay starts from the latest checkpoint on its own route, so one on another route does not vouch for it.
    elsewhere = resumed(replay_basis(None, [first, waited, skipping]), route="other-route", run_id="elsewhere")
    assert configured([first, waited, skipping, elsewhere]).basis == ""

    # An approval continuation records what the paused request replayed, so its checkpoints stay reusable.
    paused_settings = configured([first])
    waiting = Message(role="assistant", content="Waiting for approval")
    record_native_checkpoint(waiting, [], paused_settings)
    paused = _completed_run("paused", messages=[Message(role="user", content="Go"), waiting])
    restored = restore_native_history(responses_adapter(tmp_path, store=False), persisted_run=paused, session=None)
    assert restored is not None
    assert restored.native_compaction is not None
    assert restored.native_compaction.basis == paused_settings.basis


@pytest.mark.asyncio
async def test_native_route_names_the_summary_the_log_replays(tmp_path: Path) -> None:
    """A summary left in the session row cannot name the route checkpoints are recorded under."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = responses_adapter(tmp_path, store=False)
    later = _completed_run("later", messages=[Message(role="user", content="Later")])
    seeded = _session(
        "session",
        runs=[_completed_run("folded", messages=[Message(role="user", content="Earlier")]), later],
        summary=ScopeSummary(text="Stale summary.", updated_at=datetime.now(UTC)),
    )
    db = create_state_storage("test_agent", tmp_path)
    seed_session(db, seeded)
    compact_runs(db, seeded, _SCOPE, ["folded"], "Archived summary.")
    session = db.load_session("session", "agent")
    assert session is not None
    resolved = _inputs(config)
    prepared = await prepare_scope_history(
        model=model,
        resolved_inputs=resolved,
        non_prompt_tokens=0,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(_SCOPE, db, session, "session"),
    )

    assert session.summary is not None
    assert session.summary.text == "Archived summary."
    expected = configure_native_history(
        responses_adapter(tmp_path, store=False),
        plan=resolved.execution_plan,
        history_settings=resolved.history_settings,
        session=session,
        allowed=True,
    )
    assert expected is not None
    assert expected.native_compaction is not None
    assert prepared.native_history is not None
    assert prepared.native_history.native_compaction is not None
    assert prepared.native_history.native_compaction.route == expected.native_compaction.route
    # A checkpoint records the history its request replayed, so text compaction can tell where it still stands in.
    answer = Message(role="assistant", content="Done")
    record_native_checkpoint(
        answer,
        [{"type": "compaction", "content": "Checkpoint."}],
        prepared.native_history.native_compaction,
    )
    assert checkpoint_summary(answer, replay_basis("Archived summary.", [later])) == "Checkpoint."


@pytest.mark.asyncio
async def test_team_native_activation_uses_team_model(tmp_path: Path) -> None:
    """A team's native policy belongs to its leader model and the team's settings, not to its member's."""
    config, paths = _make_config(
        tmp_path,
        compaction=CompactionOverrideConfig(threshold_tokens=30000),
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000),
            "member": ModelConfig(provider="openai", id="gpt-6-luna", context_window=200000),
        },
    )
    # The member compacts on its own model at its own threshold; neither may reach the team's requests.
    config.agents["test_agent"].model = "member"
    config.teams["team"] = TeamConfig(agents=["test_agent"], display_name="Team", role="Coordinate work.")
    leader = responses_adapter(tmp_path, store=False)
    db = create_state_storage("test_agent", tmp_path)
    session = _team_session("session", team_id="team", runs=[_completed_team_run("run", team_id="team")])
    seed_session(db, session)

    prepared = await prepare_bound_scope_history(
        member_names=["test_agent"],
        model=leader,
        team_name="team",
        config=config,
        runtime_paths=paths,
        scope_context=ScopeSessionContext(HistoryScope(kind="team", scope_id="team"), db, session, "session"),
        active_model_name="default",
        active_context_window=200000,
        static_prompt_tokens=100,
        non_prompt_tokens=0,
    )

    assert prepared.native_history is not None
    native = prepared.native_history.native_compaction
    assert native is not None
    expected = leader.native_compaction_settings(threshold=120000)
    assert expected is not None
    assert native == replace(expected, basis=replay_basis(None, session.runs))
    member = responses_adapter(tmp_path, "gpt-6-luna", store=False).native_compaction_settings(threshold=30000)
    assert member is not None
    assert (native.route, native.threshold) != (member.route, member.threshold)


@pytest.mark.parametrize(
    ("provider_data", "portable"),
    [
        pytest.param({"mindroom_portable_replay": True}, True, id="saved-canonical"),
        pytest.param(
            {
                "mindroom_portable_replay": False,
                "mindroom_response_output": [{"type": "reasoning", "encrypted_content": "opaque"}],
            },
            False,
            id="saved-continuation-wins",
        ),
        pytest.param({}, False, id="legacy-unknown-policy"),
        pytest.param({"mindroom_portable_replay": None}, False, id="null-policy"),
        pytest.param({"mindroom_portable_replay": 1}, False, id="integer-policy"),
        pytest.param({"mindroom_portable_replay": "true"}, False, id="string-policy"),
        pytest.param({"mindroom_portable_replay": {}}, False, id="object-policy"),
        pytest.param({"mindroom_response_stored": False}, True, id="legacy-unstored"),
        pytest.param(
            {"mindroom_native_compaction": {"route": "old-route", "threshold": 1024}},
            True,
            id="legacy-native",
        ),
        pytest.param(
            {"mindroom_native_compaction": {"route": "old-route", "threshold": True}},
            False,
            id="malformed-native",
        ),
        pytest.param(
            {"mindroom_native_compaction": {"route": "", "threshold": 1024}},
            False,
            id="empty-native-route",
        ),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": "opaque"}]},
            True,
            id="legacy-ordered-reasoning",
        ),
        pytest.param({"mindroom_response_output": []}, False, id="empty-ordered-output"),
        pytest.param({"mindroom_response_output": [None]}, False, id="malformed-ordered-output"),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": ""}]},
            False,
            id="empty-reasoning",
        ),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": True}]},
            False,
            id="malformed-reasoning",
        ),
    ],
)
def test_restore_portable_policy_requires_latest_provenance(
    tmp_path: Path,
    provider_data: dict[str, Any],
    *,
    portable: bool,
) -> None:
    """Without legacy provenance the original budget is unknown; retain stored continuation."""
    messages = [
        Message(
            role="assistant",
            content="Older answer",
            provider_data={"response_id": "resp_older", "mindroom_portable_replay": not portable},
        ),
        Message(
            role="assistant",
            content="Latest answer",
            provider_data={"response_id": "resp_latest", "mindroom_response_stored": True, **provider_data},
        ),
        Message(role="user", content="Continue"),
    ]
    model = responses_adapter(tmp_path, store=True)
    restored = restore_native_history(model, persisted_run=_completed_run("paused", messages=messages), session=None)
    sent = _sent(model, messages, restored)
    if portable:
        assert _continues(sent) is None
    else:
        assert _continues(sent) == "resp_latest"
        assert sent["input"] == [{"role": "user", "content": "Continue"}]


@pytest.mark.parametrize("summary", ["S1", ""])
def test_native_route_identity_hashes_the_rendered_summary(tmp_path: Path, summary: str) -> None:
    """A checkpoint recorded while the summary lived in the system prompt is not reused once it is a message."""
    config, _paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    resolved = _inputs(config)
    session = _session("session", summary=ScopeSummary(text=summary, updated_at=datetime.now(UTC)) if summary else None)
    model = responses_adapter(tmp_path, store=False)
    configured = configure_native_history(
        model,
        plan=resolved.execution_plan,
        history_settings=resolved.history_settings,
        session=session,
        allowed=True,
    )

    def route_for(history_generation: str) -> str:
        native = model.native_compaction_settings(threshold=120000, history_generation=history_generation)
        assert native is not None
        return native.route

    assert configured is not None
    assert configured.native_compaction is not None
    rendered = str(compaction_summary_message(summary, run_local=False).content) if summary else ""
    assert configured.native_compaction.route == route_for(rendered)
    if summary:
        assert configured.native_compaction.route != route_for(summary)


def test_compaction_marker_names_the_newest_summary_and_native_checkpoint(tmp_path: Path) -> None:
    """A provider compaction changes the marker as a text compaction does, so inlined memory reloads after either."""
    native = responses_adapter(tmp_path, store=False).native_compaction_settings(threshold=1024)
    assert native is not None

    def answer(items: list[dict[str, Any]]) -> Message:
        reply = Message(role="assistant", content="Answer")
        record_native_checkpoint(reply, items, native)
        return reply

    def run(run_id: str, reply: Message) -> RunRecord:
        return _completed_run(run_id, messages=[Message(role="user", content="question"), reply])

    first, second = (answer([{"type": "compaction", "encrypted_content": name}]) for name in ("first", "second"))
    session = _session("session", runs=[run("plain", answer([]))])
    assert compaction_marker(session, _SCOPE) == (None, None)

    # Every completed response records its route; only a compaction adds checkpoint items.
    session.runs = [run("first", first), run("second", second), run("later", answer([]))]
    session.summary = ScopeSummary(text=" Text summary. ", updated_at=datetime.now(UTC))
    assert compaction_marker(session, _SCOPE) == ("Text summary.", second.id)


def test_history_reads_the_routes_native_settings_through_the_request_pipeline(tmp_path: Path) -> None:
    """The provider the registry builds hands history planning its route's native history and replay estimator."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={
            "default": ModelConfig(
                provider="openai",
                id="gpt-6-astra",
                api="responses",
                context_window=200000,
                extra_kwargs={"store": False},
            ),
            "chat": ModelConfig(provider="openai", id="gpt-6-astra", api="chat_completions", context_window=200000),
        },
    )
    resolved = _inputs(config)

    def configured(model: ModelProvider) -> NativeHistory | None:
        return configure_native_history(
            model,
            plan=resolved.execution_plan,
            history_settings=resolved.history_settings,
            session=None,
            allowed=True,
        )

    provider = build_model_provider(config, paths, "default")
    adapter = bare_adapter(provider)

    native = configured(provider)
    assert native is not None
    assert native.native_compaction is not None
    assert native == configured(adapter)
    assert provider.replay_estimator is adapter
    chat = build_model_provider(config, paths, "chat")
    assert (configured(chat), chat.replay_estimator) == (None, None)
