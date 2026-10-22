"""An approval resume sends the native replay policy the paused request ran with, when its route still holds.

Under that policy the request replays the stored checkpoint in place of the transcript it covers. The agent cases
resume a stored paused run; the team case pauses a ``squad`` turn on its checker's approval and resumes it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import agents
from mindroom.approval.resume import ResumeDecisions, ResumeRunner
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, NativeCompactionReplay, TurnCompleted
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.native_compaction import _NATIVE_CHECKPOINT_KEY, record_native_checkpoint
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, seed_session, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.history_helpers import _team_session
from tests.identity_helpers import entity_ids
from tests.providers.openai_family_support import responses_adapter
from tests.team_turn_helpers import SESSION_ID, delegate, member_approval, squad_turn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.llm.provider import ModelRequest, StreamEvent

pytestmark = pytest.mark.asyncio

_TRANSCRIPT = "Original canonical transcript"
_CHECKPOINT = {"type": "compaction", "id": "cmp_1", "encrypted_content": "checkpoint-bytes"}


@dataclass(frozen=True)
class _Compaction:
    """A route that compacts natively; ``route`` is what the rebuilt model would replay."""

    route: str

    def native_compaction_settings(
        self,
        *,
        threshold: int | None,
        history_generation: str,
        allow_authored: bool,
    ) -> NativeCompactionReplay | None:
        del history_generation, allow_authored
        if threshold is None:
            return None
        return NativeCompactionReplay(route=self.route, threshold=threshold)


class _RecordingModel(ScriptedModel):
    """A scripted provider that records each response's replay policy on it, as the Responses adapter does."""

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the next scripted turn, its completed response carrying the request's native settings."""
        async for event in super().stream(request):
            if isinstance(event, TurnCompleted):
                event.message.provider_data = {"mindroom_portable_replay": request.portable_replay}
                record_native_checkpoint(event.message, [], request.native_compaction)
            yield event


def _compacting_model(
    route: str = "route-a",
    turns: Sequence[Sequence[ScriptStep]] = (answer("Ready"),),
) -> ScriptedModel:
    """Return a scripted provider whose route compacts natively."""
    return _RecordingModel(
        list(turns),
        capabilities=Capabilities(native_compaction=True),
        native_history_route=_Compaction(route),
    )


def _paused_record(*, saved: dict[str, object] | None, portable: object = None) -> RunRecord:
    """Return a run paused on ``lookup`` whose last response recorded the given native provenance."""
    provider_data: dict[str, object] = {}
    if saved is not None:
        provider_data[_NATIVE_CHECKPOINT_KEY] = saved
    if portable is not None:
        provider_data["mindroom_portable_replay"] = portable
    tool = ToolExecution(tool_call_id="call-1", tool_name="lookup", tool_args={}, requires_confirmation=True)
    return RunRecord(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.paused,
        messages=[
            Message(role="system", content="You are General."),
            Message(role="user", content="Look up the port"),
            Message(
                role="assistant",
                tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
                provider_data=provider_data,
            ),
        ],
        tools=[tool],
        requirements=[RunRequirement(tool)],
    )


def _checkpointed_messages() -> list[Message]:
    """Return an earlier finished exchange whose answer route-a compacted natively, after the transcript it covers."""
    checkpoint = {"route": "route-a", "threshold": 1024, "checkpoint_prefix": True, "items": [_CHECKPOINT]}
    return [
        Message(role="user", content=_TRANSCRIPT),
        Message(role="assistant", content="Noted.", provider_data={_NATIVE_CHECKPOINT_KEY: checkpoint}),
    ]


def _checkpointed_run() -> RunRecord:
    """Return an earlier finished run of ``general`` holding the checkpointed exchange."""
    return RunRecord(
        run_id="run-0",
        agent_id="general",
        session_id="session-1",
        status=RunStatus.completed,
        messages=_checkpointed_messages(),
    )


def _responses_input(tmp_path: Path, request: ModelRequest) -> list[Any]:
    """Return the ``input`` a Responses route sends for ``request``, the projection that replays checkpoints."""
    return responses_adapter(tmp_path)._request_kwargs(request)["input"]


async def _resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
    paused: RunRecord,
    *,
    history: tuple[RunRecord, ...] = (),
) -> AgentBuild:
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                "memory": {"backend": "none"},
                "agents": {"general": {"display_name": "General", "tools": ["calculator"]}},
                "models": {"default": {"provider": "synthetic", "id": "synthetic"}},
            },
        ),
        paths,
    )
    entity_ids(config, paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda *_args, **_kwargs: model)
    store = create_session_storage("general", config, paths, identity)
    build = agents.create_agent("general", config, paths, identity, history_storage=store)
    session = new_scope_session(session_id="session-1", scope_id="general", is_team=False)
    for run in (*history, paused):
        store.save_run(session, run, record_usage=False)
    runner = ResumeRunner(
        build=build,
        config=config,
        runtime_paths=paths,
        agent_name="general",
        model_name="default",
        store=store,
        session=session,
        scope=HistoryScope(kind="agent", scope_id="general"),
        user_id=identity.requester_id,
    )
    await runner.continue_run(paused, ResumeDecisions(denied={"call-1": "Declined"}), None)
    return build


@pytest.mark.parametrize("disable_before_pause", [False, True])
async def test_a_rebuilt_approval_resumes_the_paused_requests_native_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    disable_before_pause: bool,
) -> None:
    """The resume compacts at the paused request's threshold; a request that ran without compaction stays without.

    Compacting, it replays the stored checkpoint in place of the transcript it covers; without, the full transcript.
    """
    saved = {"route": "route-a", "threshold": None if disable_before_pause else 1024}
    model = _compacting_model()

    await _resume(tmp_path, monkeypatch, model, _paused_record(saved=saved), history=(_checkpointed_run(),))

    [request] = model.requests
    expected = None if disable_before_pause else NativeCompactionReplay(route="route-a", threshold=1024)
    assert request.native_compaction == expected
    replay = _responses_input(tmp_path, request)
    assert (_CHECKPOINT in replay) is not disable_before_pause
    assert (_TRANSCRIPT in json.dumps(replay)) is disable_before_pause


async def test_a_route_change_on_rebuild_replays_without_the_stored_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuilt model on another route cannot replay the paused route's checkpoints, so the resume sends none."""
    model = _compacting_model("route-b")
    paused = _paused_record(saved={"route": "route-a", "threshold": 1024})

    await _resume(tmp_path, monkeypatch, model, paused, history=(_checkpointed_run(),))

    [request] = model.requests
    assert request.native_compaction is None
    replay = _responses_input(tmp_path, request)
    assert _CHECKPOINT not in replay
    assert _TRANSCRIPT in json.dumps(replay)


@pytest.mark.parametrize(
    ("portable", "expected"),
    [(True, True), (False, False), (None, False), ("false", False)],
    ids=["bounded", "unbounded", "legacy", "malformed"],
)
async def test_a_rebuilt_approval_keeps_the_paused_requests_replay_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    portable: object,
    *,
    expected: bool,
) -> None:
    """Portable replay follows the provenance the paused response recorded; without it, stored continuation holds."""
    model = _compacting_model()

    await _resume(tmp_path, monkeypatch, model, _paused_record(saved=None, portable=portable))

    [request] = model.requests
    assert request.portable_replay is expected


@pytest.mark.usefixtures("squad_probe")
async def test_a_team_approval_resumes_the_paused_leader_requests_native_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resumed leader request runs under its paused request's native policy and replays the same checkpoint.

    The leader's route compacts at the team's threshold, and the team's earlier answer holds that route's checkpoint;
    both leader requests replay it in place of the transcript it covers.
    """
    leader = _compacting_model(
        turns=[delegate("call-team", "checker", "Touch it"), answer("The checker touched it.")],
    )
    team = squad_turn(
        tmp_path,
        monkeypatch,
        leader=(),
        checker=[calls(tool_call("touch", "call-touch", label="dangerous")), answer("Touched.")],
        approval_rules=[ApprovalRuleConfig(match="touch", action="require_approval")],
    )
    team.models["default"] = leader
    team.config.models["default"].context_window = 200_000
    team.config.defaults.compaction = CompactionConfig(threshold_tokens=30_000)
    earlier = TeamRunRecord(
        run_id="run-0",
        team_id="squad",
        status=RunStatus.completed,
        messages=_checkpointed_messages(),
    )
    seed_session(team.storage, _team_session(SESSION_ID, team_id="squad", runs=[earlier]))
    with pytest.raises(ResponsePausedForApproval) as raised:
        await team.stream(show_tool_calls=False)

    result = await team.resume(
        raised.value.paused,
        approved=True,
        approvals=(member_approval("call-touch", "dangerous"),),
    )

    assert isinstance(result, CompletedApprovalRun)
    paused, resumed = leader.requests
    assert paused.native_compaction == NativeCompactionReplay(route="route-a", threshold=30_000)
    assert paused.portable_replay is True
    assert (resumed.native_compaction, resumed.portable_replay) == (paused.native_compaction, paused.portable_replay)
    for request in (paused, resumed):
        replay = _responses_input(tmp_path, request)
        assert _CHECKPOINT in replay
        assert _TRANSCRIPT not in json.dumps(replay)
