"""Background jobs keep silent scheduling's delivery policy."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

import pytest

from mindroom.ai import _collect_response_body_with_trace, ai_response, stream_agent_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.delegation.background import delegation_child
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.tool_jobs.conversation import ConversationKey, current_conversation
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry_state import capture_tool_registry_snapshot, restore_tool_registry_snapshot
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import make_turn_context, unwrap_extracted_collaborator
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_actor,
    job_tool,
    pending_outcome,
    pending_outcomes,
    run_job_turn,
    start_delegation_job,
    start_job,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from mindroom.tool_system.events import ToolTraceEntry


class _ProbeTools(Toolkit):
    """One read the silent turn makes, held open until the runtime shuts down when ``hold`` is set."""

    hold: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__(name="probe", tools=[self.probe_tool])

    async def probe_tool(self) -> str:
        """Read evidence using the ordinary execution path."""
        if type(self).hold:
            await asyncio.Event().wait()
        return "Job evidence"


@contextmanager
def _registered_probe(*, hold: bool) -> Iterator[None]:
    """Register the ``probe`` toolkit for one turn and restore the registries afterwards."""
    snapshot = capture_tool_registry_snapshot()

    @register_tool_with_metadata(
        name="probe",
        file_access=ToolFileAccess.NONE,
        display_name="Probe",
        description="Reads evidence for a silent check.",
        category=ToolCategory.DEVELOPMENT,
    )
    def probe_factory() -> type[_ProbeTools]:
        return _ProbeTools

    _ProbeTools.hold = hold
    try:
        yield
    finally:
        restore_tool_registry_snapshot(snapshot)


@dataclass
class _SilentTurn:
    answer: str
    trace: list[ToolTraceEntry]
    recorder: TurnRecorder | None
    pending_outcomes: list[object]


async def _run_silent_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turns: Sequence[Sequence[ScriptStep]],
    *,
    enabled: bool,
    collect_stream: bool,
    record_turn: bool = True,
    direct_stream: bool = False,
    hold_probe: bool = False,
) -> _SilentTurn:
    """Run one silent scheduled agent turn, through `ai_response` or the streaming entry a Matrix reply calls.

    ``hold_probe`` keeps the probe tool running until the runtime shuts down, so a released call returns its handle.
    """
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
        agents={"leader": AgentConfig(display_name="Leader", tools=["probe"])},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    context = replace(_delegate_runtime_context(config, paths), source_kind=SILENT_SCHEDULE_SOURCE_KIND)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    ScriptedAgents({"leader": ScriptedModel(list(turns))}).install(monkeypatch)
    ctx = replace(
        make_turn_context(
            entity_label="leader",
            session_id=context.session_id,
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            requester_id=owner.requester_id,
        ),
        allow_no_report_response=True,
    )
    trace: list[ToolTraceEntry] = []
    recorder = TurnRecorder(user_message="Silent check") if record_turn else None
    try:
        with tool_runtime_context(context), _registered_probe(hold=hold_probe):
            if direct_stream:
                answer_text = await _collect_response_body_with_trace(
                    stream_agent_response(
                        ctx,
                        prompt="Silent check",
                        runtime_paths=paths,
                        config=config,
                        execution_identity=owner,
                        show_tool_calls=True,
                        turn_recorder=recorder,
                    ),
                    show_tool_calls=True,
                    tool_trace_collector=trace,
                )
            else:
                answer_text = await ai_response(
                    ctx,
                    prompt="Silent check",
                    runtime_paths=paths,
                    config=config,
                    execution_identity=owner,
                    collect_streamed_response=collect_stream,
                    show_tool_calls=True,
                    tool_trace_collector=trace,
                    turn_recorder=recorder,
                )
        return _SilentTurn(answer_text, trace, recorder, pending_outcomes(runtime))
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("collect_stream", [False, True])
@pytest.mark.parametrize("final", ["NO_REPLY", "Finding", ""])
@pytest.mark.parametrize("use_tool", [False, True])
async def test_enabled_silent_turn_without_jobs_matches_a_disabled_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    collect_stream: bool,
    final: str,
    use_tool: bool,
) -> None:
    """Enabling background jobs changes nothing for a quiet turn that starts none."""
    tool_calls = [calls(tool_call("probe_tool", "read"))] if use_tool else []
    observed = []
    for enabled in (False, True):
        turn = await _run_silent_turn(
            tmp_path / str(enabled),
            monkeypatch,
            [*tool_calls, answer(final)],
            enabled=enabled,
            collect_stream=collect_stream,
        )
        assert turn.recorder is not None
        observed.append(
            (
                turn.answer,
                [(tool.tool_name, tool.result_preview) for tool in turn.trace],
                turn.recorder.outcome,
                turn.recorder.assistant_text,
                [(tool.tool_name, tool.result_preview) for tool in turn.recorder.completed_tools],
            ),
        )
    assert observed[1] == observed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collect_stream", "direct_stream"),
    [(False, False), (True, False), (True, True)],
    ids=["blocking", "collected-stream", "matrix-stream"],
)
async def test_a_turn_with_jobs_on_lets_a_managed_call_detach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    collect_stream: bool,
    direct_stream: bool,
) -> None:
    """Every reply entry point runs its managed calls as jobs once the instance runs them."""
    turn = await _run_silent_turn(
        tmp_path,
        monkeypatch,
        [calls(tool_call("probe_tool", "read", background_after=0)), answer("done")],
        enabled=True,
        collect_stream=collect_stream,
        direct_stream=direct_stream,
        hold_probe=True,
    )
    [call] = [tool for tool in turn.trace if tool.tool_name == "probe_tool"]
    assert '"job_id"' in str(call.result_preview)


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
async def test_quiet_and_visible_results_stay_with_their_own_replies(tmp_path: Path, *, silent: bool) -> None:
    """A silent schedule's reply hears only of quiet results, and an ordinary reply only of visible ones."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    envelope = _plain_request(_target(thread_id="$thread")).response_envelope
    if silent:
        envelope = replace(envelope, origin=replace(envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND))
    context = runner.deps.tool_runtime.build_context(
        envelope.target,
        user_id=envelope.requester_id,
        source_envelope=envelope,
    )
    assert context is not None
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "result")

    try:
        for name, kind in (("quiet", SILENT_SCHEDULE_SOURCE_KIND), ("visible", "message")):
            await start_job(
                runtime,
                name,
                tool_name="tool",
                depth=0,
                source_kind=kind,
                adapter={},
                owner=owner,
                operation=operation,
            )
            waited = await runtime.wait(name, owner=owner, depth=0)
            await runtime.release_wait(name, waited.claim)
        with tool_runtime_context(context):
            current = current_conversation()
        assert current is not None
        assert [job.job_id for job in await current[0].ended_jobs(current[1])] == ["quiet" if silent else "visible"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_accepted_job_persists_silent_delivery_policy_across_restart(tmp_path: Path, *, native: bool) -> None:
    """SDK and native delegated calls stay with quiet replies after a restart, apart from visible ones."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    envelope = _plain_request(_target(thread_id="$thread")).response_envelope
    envelope = replace(envelope, origin=replace(envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND))
    context = runner.deps.tool_runtime.build_context(
        envelope.target,
        user_id=envelope.requester_id,
        source_envelope=envelope,
    )
    assert context is not None
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "No findings"

    async def native_operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", await slow())

    model = ScriptedModel([calls(tool_call("slow", "call", background_after=0)), answer("NO_REPLY")])
    actor = job_actor(job_tool(slow), name="general")
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                if native:
                    child = replace(delegation_child(completed_delegation_job()), caller_agent_name="general")
                    accepted = await start_delegation_job(runtime, child, owner=owner, operation=native_operation)
                    job_id = accepted.job_id
                else:
                    response = await run_job_turn(actor, model, "Check quietly", session_id=owner.session_id or "")
                    job_id = json.loads(str(response.tools[0].result))["job_id"]
            release.set()
            waited = await runtime.wait(job_id, owner=owner, depth=0)
            await runtime.release_wait(job_id, waited.claim)
        await runtime.shutdown()
        runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
        await runtime.recover()
        restored = pending_outcome(runtime, job_id)
        assert restored is not None
        assert restored.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        assert owner.room_id is not None
        assert owner.requester_id is not None
        conversation = {
            "recipient": owner.recipient,
            "room_id": owner.room_id,
            "thread_id": owner.resolved_thread_id,
            "requester_id": owner.requester_id,
        }
        quiet = await runtime.ended_jobs(ConversationKey(**conversation, silent=True))
        assert [job.job_id for job in quiet] == [job_id]
        assert await runtime.ended_jobs(ConversationKey(**conversation, silent=False)) == ()
    finally:
        release.set()
        await runtime.shutdown()
