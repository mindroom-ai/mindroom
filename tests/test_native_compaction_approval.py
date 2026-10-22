"""An approval resume sends the native replay policy the paused request ran with, when its route still holds.

The team continuation still runs through Agno until the team runtime cutover; its native replay is tested with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom import agents
from mindroom.approval.resume import ResumeDecisions, ResumeRunner
from mindroom.config.main import Config
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import Capabilities, NativeCompactionReplay
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.native_compaction import _NATIVE_CHECKPOINT_KEY
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild

pytestmark = pytest.mark.asyncio


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


def _compacting_model(route: str = "route-a") -> ScriptedModel:
    """Return a scripted provider whose route compacts natively."""
    return ScriptedModel(
        [answer("Ready")],
        capabilities=Capabilities(native_compaction=True),
        native_compaction=_Compaction(route),
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


async def _resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
    paused: RunRecord,
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
    store.save_run(session, paused, record_usage=False)
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
    """The resume compacts at the paused request's threshold; a request that ran without compaction stays without."""
    saved = {"route": "route-a", "threshold": None if disable_before_pause else 1024}
    model = _compacting_model()

    await _resume(tmp_path, monkeypatch, model, _paused_record(saved=saved))

    [request] = model.requests
    expected = None if disable_before_pause else NativeCompactionReplay(route="route-a", threshold=1024)
    assert request.native_compaction == expected


async def test_a_route_change_on_rebuild_replays_without_the_stored_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuilt model on another route cannot replay the paused route's checkpoints, so the resume sends none."""
    model = _compacting_model("route-b")

    await _resume(tmp_path, monkeypatch, model, _paused_record(saved={"route": "route-a", "threshold": 1024}))

    [request] = model.requests
    assert request.native_compaction is None


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
