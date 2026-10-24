"""Each reply publishes the ``io.mindroom.ai_run`` metadata MindRoom published for the same run under Agno 3.0.9.

``tests/fixtures/agno_parity/events/ai_run.json`` holds the metadata each captured run published, keyed by run id.
A new run replays through the entry point that published it, ``ai_response``, ``stream_agent_response``,
``team_response``, or ``team_response_stream``, with a run-metadata collector. Each scripted model answers every
request as the run's stored assistant messages did, with their usage and tool calls, or fails as a failed run did,
and the history preparation returns what the run's stored metadata says it prepared: the capture kept that result,
not the stored history and prompt it came from. An approval resume prepares no history, so the resumes run from the
stores the capture dumped just before them: the team's here, and the agent's in ``tests/test_approval_resume.py``.
A run's duration is a clock reading, so only its presence is compared.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mindroom import execution_preparation
from mindroom.ai import ai_response, stream_agent_response
from mindroom.config.main import Config
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.history.types import CompactionDecision, PreparedHistoryState, ResolvedReplayPlan
from mindroom.llm.errors import ProviderError
from mindroom.llm.runs import AnyRunRecord, RunRecord, RunStatus, TeamRunRecord
from mindroom.message_target import MessageTarget
from mindroom.response_turn import CompletedApprovalRun, paused_attempt_from_response
from mindroom.teams.resume import blocking_team_pause
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.ai_run_golden_helpers import GOLDEN_RUN_METADATA, golden_metadata, published_as_compared
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    make_turn_context,
    test_runtime_paths,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls
from tests.llm.agno_dict_helpers import stored_run_record
from tests.team_runtime.squad import SQUAD_APPROVED, STORAGE, approval, scripted
from tests.team_turn_helpers import authored_team_config, build_team_turn, install_team_models

_FIXTURES = Path(__file__).parent / "fixtures" / "agno_parity"
_REQUESTER = "@alice:example.org"
# The capture's agents, teams, and models, as far as the published metadata reads them: the ``code`` agent's model
# name, display name, and context window, and the teams' leader model and its window.
_AUTHORED = authored_team_config(
    {
        "code": {
            "display_name": "Code",
            "role": "Writes code.",
            "model": "code",
            "tools": ["calculator", "attachments"],
        },
        "writer": {"display_name": "Writer", "role": "Writes.", "model": "writer-model", "tools": ["calculator"]},
        "checker": {"display_name": "Checker", "role": "Checks.", "model": "checker-model", "tools": ["calculator"]},
    },
    teams={
        "squad": {"display_name": "Squad", "role": "Answers.", "agents": ["writer", "checker"], "model": "leader"},
        "panel": {
            "display_name": "Panel",
            "role": "Answers.",
            "agents": ["writer", "checker"],
            "model": "leader",
            "mode": "collaborate",
        },
    },
    models=("default", "code", "leader", "writer-model", "checker-model"),
)
_AUTHORED["models"]["code"] |= {"display_name": "Code Model", "context_window": 200000}
_AUTHORED["models"]["leader"]["context_window"] = 100000
_AUTHORED["tool_approval"] = {"default": "auto_approve", "rules": [{"match": "multiply", "action": "require_approval"}]}
# The runs whose replies streamed; the rest were blocking.
_STREAMED = {"run-tools", "panel-completed"}
# The approval resumes, which ``tests/test_approval_resume.py`` publishes for the agent and this module for the team.
_RESUMED = {"run-approved", "run-rejected", "run-delegation-approved", "team-approved"}
# An edit cut removed this run's row, so no stored record remains to replay it from.
_WITHOUT_RECORD = {"run-c4"}


def _stored_runs() -> dict[str, AnyRunRecord]:
    """Return each run row the run goldens hold, by run id, skipping values nested in a row."""
    runs: dict[str, AnyRunRecord] = {}
    for name in ("run_output.json", "team_run_output.json"):
        for key, data in json.loads((_FIXTURES / "runs" / name).read_text(encoding="utf-8")).items():
            _, path = key.split(":", 1)
            if path == data["run_id"]:
                assert path not in runs
                runs[path] = stored_run_record(data)
    return runs


_RUNS = _stored_runs()


def _prepared_history(record: AnyRunRecord) -> PreparedHistoryState:
    """Return the prepared history whose metadata the run kept when it started."""
    content = (record.metadata or {})[AI_RUN_METADATA_KEY]
    compaction = content["compaction"]
    plan = compaction["replay_plan"]
    return PreparedHistoryState(
        replay_plan=ResolvedReplayPlan(
            mode=plan["mode"],
            estimated_tokens=plan["estimated_tokens"],
            add_history_to_context=True,
        ),
        compaction_decision=CompactionDecision(
            mode=compaction["decision"],
            reason=compaction["reason"],
            current_history_tokens=compaction.get("current_history_tokens"),
            trigger_budget_tokens=compaction.get("trigger_budget_tokens"),
            hard_budget_tokens=compaction.get("hard_budget_tokens"),
            fitted_replay_tokens=compaction.get("fitted_replay_tokens"),
        ),
        compaction_reply_outcome=compaction["outcome"],
        prepared_context_tokens=content["prepared_context"]["tokens"],
    )


def _prepares(monkeypatch: pytest.MonkeyPatch, record: AnyRunRecord) -> None:
    """Make the history preparation return what ``record``'s metadata says the run prepared."""
    prepared = _prepared_history(record)

    async def finalize(**_kwargs: object) -> PreparedHistoryState:
        return prepared

    monkeypatch.setattr(execution_preparation, "_finalize_prepared_history", finalize)


async def _fail(message: str) -> None:
    raise ProviderError(message, provider="Synthetic")


def _model(record: AnyRunRecord) -> ScriptedModel:
    """Return a model that answers each request as ``record``'s assistant messages did, or fails as the run did."""
    assert record.model is not None
    if record.status is RunStatus.error:
        return scripted(record.model, [Await(lambda: _fail(str(record.content)))])
    turns: list[list[ScriptStep]] = []
    for message in record.messages:
        if message.role != "assistant":
            continue
        if message.tool_calls:
            turns.append(calls(*message.tool_calls, metrics=message.metrics))
        else:
            turns.append(answer(str(message.content or ""), message.metrics))
    return scripted(record.model, *turns)


def _resumed_model(record: AnyRunRecord) -> ScriptedModel:
    """Return a model that answers a resumed request as ``record``'s last assistant message did."""
    assert record.model is not None
    last = [message for message in record.messages if message.role == "assistant"][-1]
    return scripted(record.model, answer(str(last.content), last.metrics))


def _member_runs(run_id: str) -> list[RunRecord]:
    return [run for run in _RUNS.values() if isinstance(run, RunRecord) and run.parent_run_id == run_id]


async def _agent_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> dict[str, Any]:
    """Replay the agent run ``run_id`` through its reply's entry point and return the metadata it published."""
    record = _RUNS[run_id]
    # MindRoom's interrupted-turn record replaced the failed run's row and keeps no usage, model, or provider. The
    # failed run's own record held what Agno stored for the same failing request when no turn record replaced it.
    replayed = _RUNS["run-native-error"] if run_id == "run-error" else record
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(_AUTHORED), paths)
    install_team_models(monkeypatch, {"code": _model(replayed)})
    _prepares(monkeypatch, record)
    assert record.session_id is not None
    room_id, _, thread_id = record.session_id.rpartition(":")
    runtime = make_test_tool_runtime_context(
        agent_name="code",
        target=MessageTarget(
            room_id=room_id,
            source_thread_id=thread_id,
            resolved_thread_id=thread_id,
            reply_to_event_id="$current",
            session_id=record.session_id,
        ),
        requester_id=_REQUESTER,
        client=SimpleNamespace(user_id="@mindroom_code:example.org"),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        correlation_id="correlation-1",
        membership_turn_id="$current",
    )
    ctx = make_turn_context(
        "code",
        session_id=record.session_id,
        run_id=run_id,
        correlation_id="correlation-1",
        reply_to_event_id="$current",
        room_id=room_id,
        thread_id=thread_id,
        requester_id=_REQUESTER,
    )
    published: dict[str, Any] = {}
    reply = {
        "ctx": ctx,
        "prompt": "Hello",
        "runtime_paths": paths,
        "config": config,
        "execution_identity": build_execution_identity_from_runtime_context(runtime),
        "run_metadata_collector": published,
    }
    with tool_runtime_context(runtime):
        if run_id in _STREAMED:
            _ = [chunk async for chunk in stream_agent_response(**reply)]
        else:
            await ai_response(**reply)
    return published


async def _team_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str) -> dict[str, Any]:
    """Replay the team run ``run_id`` through its reply's entry point and return the metadata it published."""
    record = _RUNS[run_id]
    assert isinstance(record, TeamRunRecord)
    assert record.team_id is not None
    mode = TeamMode(_AUTHORED["teams"][record.team_id].get("mode", "coordinate"))
    team = build_team_turn(
        tmp_path,
        monkeypatch,
        _AUTHORED,
        members=("writer", "checker"),
        team_name=record.team_id,
        mode=mode,
    )
    team.models["leader"] = _model(record)
    for member in _member_runs(run_id):
        assert member.model is not None
        team.models[member.model] = _model(member)
    _prepares(monkeypatch, record)
    published: dict[str, Any] = {}
    ctx = team.ctx(session_id=record.session_id, run_id=run_id)
    if run_id in _STREAMED:
        await team.stream(ctx=ctx, run_metadata_collector=published)
    else:
        await team.respond(ctx=ctx, run_metadata_collector=published)
    return published


def test_only_the_cut_run_has_no_stored_record() -> None:
    """Every golden run but the one an edit cut removed has a stored record to replay it from."""
    assert {run_id for run_id in GOLDEN_RUN_METADATA if run_id not in _RUNS} == _WITHOUT_RECORD


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", sorted(set(GOLDEN_RUN_METADATA) - _WITHOUT_RECORD - _RESUMED))
async def test_a_reply_publishes_the_golden_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """A text, tool, media, failed, compacted, or team run's reply publishes the metadata Agno's reply did."""
    reply = _team_reply if isinstance(_RUNS[run_id], TeamRunRecord) else _agent_reply
    published = await reply(tmp_path, monkeypatch, run_id)
    assert published_as_compared(published) == golden_metadata(run_id)


@pytest.mark.asyncio
async def test_a_team_approval_resume_publishes_the_golden_run_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The team paused on its checker's ``multiply`` resumes from its stored pause and publishes Agno's metadata.

    The leader and the checker answer their resumed requests as their stored runs' last assistant messages did.
    """
    team = build_team_turn(tmp_path, monkeypatch, _AUTHORED, members=("writer", "checker"), team_name="squad")
    [checker] = _member_runs("team-approved")
    team.models["leader"] = _resumed_model(_RUNS["team-approved"])
    team.models["checker-model"] = _resumed_model(checker)
    store = team.storage
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store.path)) as connection:
        connection.executescript((STORAGE / "team_squad__before_team-approved.sql").read_text(encoding="utf-8"))
    paused = store.get_run("team-approved")
    assert isinstance(paused, TeamRunRecord)
    assert paused.status is RunStatus.paused
    attempt = paused_attempt_from_response(
        paused,
        fallback_session_id=SQUAD_APPROVED,
        fallback_run_id=paused.run_id,
        toolkit_owners={("checker", "multiply"): "calculator"},
    )
    assert attempt is not None
    # The pause the blocking reply showed, whose document the resume continues.
    shown = blocking_team_pause(
        attempt,
        paused,
        paused.member_responses,
        config_names=team.member_names,
        display_names=["Writer", "Checker"],
        show_tool_calls=False,
    )
    [requirement] = paused.requirements or []
    call = requirement.tool_execution
    assert call.tool_call_id is not None
    assert call.tool_name is not None

    result = await team.resume(
        shown,
        approved=True,
        approvals=[approval(call.tool_call_id, call.tool_name, call.tool_args or {}, agent="checker")],
        session_id=SQUAD_APPROVED,
        model_name="leader",
    )

    assert isinstance(result, CompletedApprovalRun)
    assert published_as_compared(result.metadata_content) == golden_metadata("team-approved")
