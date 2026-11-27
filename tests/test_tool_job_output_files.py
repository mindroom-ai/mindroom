"""Workspace output policies cover managed result retrieval."""

from __future__ import annotations

import asyncio
import json
import threading
from ast import literal_eval
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

import pytest
import pytest_asyncio

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools import job as job_module
from mindroom.delegation.background import delegation_outcome
from mindroom.delegation.cost import SubagentCost, received_subagent_cost_usd, receiving_subagent_cost
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.llm.events import RunFinished
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunStatus
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.run_storage import RunStorage
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs import consumption as consumption_module
from mindroom.tool_jobs.consumption import session_state_delta, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, read_result_payload
from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundOutcome, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.authoring import Toolkit, current_call
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import AdaptedToolkit, ToolValue
from mindroom.tool_system.toolkits import adapt_toolkit
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    always_owned,
    lookup,
    persist_to,
    run_built_turn,
    start_delegation_job,
    start_job,
    tool_job_runtime,
)
from tests.turn_model_helpers import turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.constants import RuntimePaths
    from mindroom.llm.runs import RunRecord
    from mindroom.tool_system.output_files import ToolOutputFileRequest
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from tests.fakes.scripted_model import ScriptStep


_LARGE_RESULT = "Full output: café 🦀\n" * 10_000
pytestmark = pytest.mark.asyncio


@dataclass
class _OutputAgent:
    build: AgentBuild
    model: ScriptedModel
    runtime: ToolJobRuntime
    owner: ToolExecutionIdentity
    workspace: Path
    paths: RuntimePaths
    config: Config

    async def call(self, name: str, **arguments: object) -> RunRecord:
        self.model.turns.extend([calls(tool_call(name, "output-call", **arguments)), answer("done")])
        return await run_built_turn(
            self.build,
            config=self.config,
            runtime_paths=self.paths,
            prompt="retrieve output",
            session_id=self.owner.session_id or "session",
        )

    async def save_job(self) -> str:
        async def operation() -> BackgroundOutcome:
            payload = encode_result_payload(ToolResultPayload(_LARGE_RESULT))
            return BackgroundOutcome("completed", _LARGE_RESULT, result_payload=payload)

        job_id = "a" * 64
        await start_job(
            self.runtime,
            job_id,
            tool_name="saved_output",
            depth=0,
            adapter={},
            owner=self.owner,
            operation=operation,
        )
        ready = await self.runtime.wait(job_id, owner=self.owner, depth=0)
        await self.runtime.release_wait(job_id, ready.claim)
        return job_id

    async def save_payload_job(
        self,
        job_id: str,
        payload: ToolResultPayload,
        summary: str = "done",
        *,
        status: Literal["completed", "failed"] = "completed",
        tool_name: str = "saved_output",
    ) -> str:
        async def operation() -> BackgroundOutcome:
            return BackgroundOutcome(status, summary, result_payload=encode_result_payload(payload))

        await start_job(
            self.runtime,
            job_id,
            tool_name=tool_name,
            depth=0,
            adapter={},
            owner=self.owner,
            operation=operation,
        )
        ready = await self.runtime.wait(job_id, owner=self.owner, depth=0)
        await self.runtime.release_wait(job_id, ready.claim)
        return job_id

    async def owned_turn(
        self,
        *steps: Sequence[ScriptStep],
        session_state: dict[str, object] | None = None,
        run_id: str = "owned-run",
        extra_toolkits: tuple[AdaptedToolkit, ...] = (),
    ) -> tuple[RunRecord, dict[str, object], float | None]:
        store = create_agent_state_storage("leader", self.paths.storage_root)
        session = ConversationSession(
            session_id=self.owner.session_id or "session",
            kind="agent",
            owner_id="leader",
            session_state={} if session_state is None else dict(session_state),
        )
        cost = SubagentCost()
        self.model.turns.extend(steps)
        hooks = agent_loop_hooks(
            self.build,
            HookTurn(
                config=self.config,
                runtime_paths=self.paths,
                model_name="default",
                storage=RunStorage(store=store, session=session, scope=None),
                replay_plan=None,
                prompt_id=None,
            ),
        )

        @owned_tool_execution(enabled=always_owned)
        async def run() -> RunRecord:
            set_consumption_storage(lambda: store)
            finished = [
                event.record
                async for event in run_agent(
                    replace(
                        self.build.spec,
                        system_messages=prompt_for_turn(self.build),
                        toolkits=(*self.build.spec.toolkits, *extra_toolkits),
                    ),
                    [Message(role="user", content="retrieve output")],
                    ctx=RunContext(run_id=run_id, session=session),
                    hooks=hooks,
                    request_template=self.build.request_template,
                    persist=persist_to(store, session),
                )
                if isinstance(event, RunFinished)
            ]
            return finished[-1]

        with receiving_subagent_cost(cost):
            record = await run()
            return record, session.session_state, received_subagent_cost_usd()


@asynccontextmanager
async def _output_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools: tuple[str, ...] = (),
    max_bytes: int | None = None,
) -> AsyncIterator[_OutputAgent]:
    """Build a workspace agent with the owned loop's dispatch and isolated durable jobs."""
    paths = _runtime_paths(tmp_path)
    if max_bytes is not None:
        paths = replace(paths, process_env={"MINDROOM_TOOL_OUTPUT_REDIRECT_MAX_BYTES": str(max_bytes)})
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"], tools=list(tools))},
        models={"default": {"provider": "openai", "id": "gpt-6-astra"}},
        memory={"backend": "file"},
        defaults={"tools": []},
    )
    bind_runtime_paths(config, runtime_paths=paths)
    context = _delegate_runtime_context(config, paths)
    owner = context.execution_identity
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = ScriptedModel([])
    ScriptedAgents({"leader": model}).install(monkeypatch)
    build = create_agent(
        "leader",
        config,
        paths,
        execution_identity=owner,
        knowledge=None,
        model=turn_model(config, paths, "leader"),
        plugins=config_plugins(config, paths),
    )
    workspace = resolve_agent_runtime("leader", config, paths, execution_identity=owner).workspace
    assert workspace is not None
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                yield _OutputAgent(build, model, runtime, owner, workspace.root, paths, config)
    finally:
        await runtime.shutdown()


@pytest_asyncio.fixture
async def output_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_OutputAgent]:
    """Build a workspace agent with the owned loop's dispatch and isolated durable jobs."""
    async with _output_agent(tmp_path, monkeypatch) as agent:
        yield agent


@pytest.mark.parametrize("explicit", [False, True])
async def test_job_retrieval_applies_workspace_output_policy(output_agent: _OutputAgent, explicit: bool) -> None:
    """Completed values redirect intact to the requested or automatic workspace file."""
    case = output_agent
    job_id = await case.save_job()
    arguments = {"mindroom_output_path": "results/job.txt"} if explicit else {}
    response = await case.call("job", action="wait", job_id=job_id, **arguments)
    assert response.tools
    assert not response.tools[0].is_error
    result = literal_eval(str(response.tools[0].result))
    receipt = result["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved = (case.workspace / receipt["path"]).read_bytes()
    assert saved == _LARGE_RESULT.encode()
    assert "truncated" not in saved.decode()
    assert len(str(response.tools[0].result)) < 12_000
    if explicit:
        assert receipt["path"] == "results/job.txt"
    else:
        assert receipt["auto_saved"] is True
    saved_job = await lookup(case.runtime, job_id, owner=case.owner, depth=0)
    assert (await read_result_payload(case.runtime, saved_job)).value == _LARGE_RESULT


async def test_job_output_path_is_validated_before_claiming_result(output_agent: _OutputAgent) -> None:
    """A bad destination neither escapes the workspace nor consumes the stored value."""
    case = output_agent
    job_id = await case.save_job()
    response = await case.call("job", action="wait", job_id=job_id, mindroom_output_path="../escape.txt")
    assert response.tools
    assert not response.tools[0].is_error
    assert literal_eval(str(response.tools[0].result))["mindroom_tool_output"]["status"] == "error"
    saved = await lookup(case.runtime, job_id, owner=case.owner, depth=0)
    assert not saved.consumed
    assert not (case.workspace.parent / "escape.txt").exists()


@pytest.mark.parametrize("explicit", [False, True])
async def test_native_delegation_job_wait_redirects_saved_result(output_agent: _OutputAgent, explicit: bool) -> None:
    """Retrieving a subagent's saved result applies the retrieval output policy."""
    case = output_agent
    child = prepare_child_turn(
        "leader",
        "leader",
        "saved output",
        owner=case.owner,
        config=case.config,
        runtime_paths=case.paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )

    async def completed() -> BackgroundOutcome:
        return delegation_outcome("completed", _LARGE_RESULT)

    job = await start_delegation_job(case.runtime, child, owner=case.owner, operation=completed)
    ready = await case.runtime.wait(job.job_id, owner=case.owner, depth=0)
    await case.runtime.release_wait(job.job_id, ready.claim)
    arguments = {"mindroom_output_path": "results/child.txt"} if explicit else {}
    response = await case.call("job", action="wait", job_id=job.job_id, **arguments)
    assert response.status is RunStatus.completed
    assert response.tools
    assert not response.tools[0].is_error
    receipt = literal_eval(str(response.tools[0].result))["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (case.workspace / receipt["path"]).read_bytes() == _LARGE_RESULT.encode()


async def test_collect_media_redirect_error_keeps_claims_state_and_cost_for_same_turn_retry(
    output_agent: _OutputAgent,
) -> None:
    """A collect redirect error leaves jobs unconsumed, state and cost untouched, and lets a same-turn retry succeed."""
    case = output_agent
    text_id = await case.save_payload_job(
        "b" * 64,
        ToolResultPayload(
            value="text-ok",
            state_delta=session_state_delta(
                {"flag": "before", "conflict": "old"},
                {"flag": "after", "conflict": "job"},
            ),
        ),
        summary="text-ok",
    )
    child = replace(
        prepare_child_turn(
            "leader",
            "leader",
            "subagent work",
            owner=case.owner,
            config=case.config,
            runtime_paths=case.paths,
            depth=0,
            budget_monitor=None,
            model=None,
            agent_mode="standard",
            previous=None,
            persona=None,
        ),
        cost_usd=0.35,
    )

    async def child_done() -> BackgroundOutcome:
        return delegation_outcome("completed", "child-answer")

    child_job = await start_delegation_job(case.runtime, child, owner=case.owner, operation=child_done)
    ready_child = await case.runtime.wait(child_job.job_id, owner=case.owner, depth=0)
    await case.runtime.release_wait(child_job.job_id, ready_child.claim)
    media_id = await case.save_payload_job(
        "c" * 64,
        ToolResultPayload(value=ToolValue(content="plot", images=(Image(content=b"png"),))),
        summary="plot",
    )
    initial_state: dict[str, object] = {"flag": "before", "conflict": "changed"}

    # Turn 1: failed redirect alone must leave every job unconsumed and state/cost untouched.
    failed_turn, state_after_error, cost_after_error = await case.owned_turn(
        calls(tool_call("job", "collect-err", action="collect", mindroom_output_path="results/collect.txt")),
        answer("redirect failed"),
        session_state=initial_state,
        run_id="collect-err-run",
    )
    assert failed_turn.tools
    err_receipt = literal_eval(str(failed_turn.tools[0].result))["mindroom_tool_output"]
    assert err_receipt["status"] == "error"
    assert state_after_error == initial_state
    assert cost_after_error is None
    assert not (case.workspace / "results/collect.txt").exists()
    for job_id in (text_id, child_job.job_id, media_id):
        assert not (await lookup(case.runtime, job_id, owner=case.owner, depth=0)).consumed

    # Turn 2: failed redirect followed immediately by a plain collect in the SAME turn succeeds and consumes all.
    retry_turn, state_after_retry, cost_after_retry = await case.owned_turn(
        calls(tool_call("job", "collect-1", action="collect", mindroom_output_path="results/collect.txt")),
        calls(tool_call("job", "collect-2", action="collect")),
        answer("collected"),
        session_state=initial_state,
        run_id="collect-retry-run",
    )
    assert len(retry_turn.tools or ()) == 2
    assert literal_eval(str(retry_turn.tools[0].result))["mindroom_tool_output"]["status"] == "error"
    retried = str(retry_turn.tools[1].result)
    assert "text-ok" in retried
    assert "Session state conflicts: conflict" in retried
    assert "child-answer" in retried
    assert "plot" in retried
    assert [img.content for message in retry_turn.messages or () for img in message.images or ()] == [b"png"]
    assert state_after_retry == {"flag": "after", "conflict": "changed"}
    assert cost_after_retry == pytest.approx(0.35)
    for job_id in (text_id, child_job.job_id, media_id):
        assert (await lookup(case.runtime, job_id, owner=case.owner, depth=0)).consumed


async def test_wait_media_redirect_error_keeps_claim_and_state_for_same_turn_retry(
    output_agent: _OutputAgent,
) -> None:
    """A wait redirect error releases the claim immediately, preserves state, and lets a same-turn retry succeed."""
    case = output_agent
    media_id = await case.save_payload_job(
        "d" * 64,
        ToolResultPayload(
            value=ToolValue(content="chart", images=(Image(content=b"png"),)),
            state_delta=session_state_delta({"counter": 1}, {"counter": 2}),
        ),
        summary="chart",
    )
    conflict_id = await case.save_payload_job(
        "e" * 64,
        ToolResultPayload(
            value="plain-report",
            state_delta=session_state_delta({"ok": 0, "clash": "old"}, {"ok": 1, "clash": "job"}),
        ),
        summary="plain-report",
    )

    # Turn 1: redirect error on media job leaves claim released and state untouched.
    failed_turn, state_after_error, _ = await case.owned_turn(
        calls(tool_call("job", "wait-err", action="wait", job_id=media_id, mindroom_output_path="results/chart.txt")),
        answer("err"),
        session_state={"counter": 1},
        run_id="wait-err-run",
    )
    assert failed_turn.tools
    assert literal_eval(str(failed_turn.tools[0].result))["mindroom_tool_output"]["status"] == "error"
    assert state_after_error == {"counter": 1}
    assert not (await lookup(case.runtime, media_id, owner=case.owner, depth=0)).consumed

    # Turn 2: failed wait redirect followed by plain wait in the SAME turn succeeds and consumes the job.
    retry_turn, state_after_retry, _ = await case.owned_turn(
        calls(tool_call("job", "wait-1", action="wait", job_id=media_id, mindroom_output_path="results/chart.txt")),
        calls(tool_call("job", "wait-2", action="wait", job_id=media_id)),
        answer("done"),
        session_state={"counter": 1},
        run_id="wait-retry-run",
    )
    assert len(retry_turn.tools or ()) == 2
    assert literal_eval(str(retry_turn.tools[0].result))["mindroom_tool_output"]["status"] == "error"
    assert str(retry_turn.tools[1].result) == "chart"
    assert [img.content for message in retry_turn.messages or () for img in message.images or ()] == [b"png"]
    assert state_after_retry == {"counter": 2}
    assert (await lookup(case.runtime, media_id, owner=case.owner, depth=0)).consumed

    # Successful redirect preserves session-state conflict warning in the redirected file and merges unconflicted keys.
    ok_turn, state_after_ok, _ = await case.owned_turn(
        calls(
            tool_call(
                "job",
                "wait-conflict",
                action="wait",
                job_id=conflict_id,
                mindroom_output_path="results/conflict.txt",
            ),
        ),
        answer("saved"),
        session_state={"ok": 0, "clash": "newer"},
        run_id="wait-conflict-run",
    )
    assert ok_turn.tools
    receipt = literal_eval(str(ok_turn.tools[0].result))["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved_text = (case.workspace / receipt["path"]).read_text(encoding="utf-8")
    assert saved_text == "plain-report\nSession state conflicts: clash"
    assert state_after_ok == {"ok": 1, "clash": "newer"}
    assert (await lookup(case.runtime, conflict_id, owner=case.owner, depth=0)).consumed


async def test_collect_auto_save_error_releases_claims_immediately_and_keeps_jobs_unconsumed(
    output_agent: _OutputAgent,
) -> None:
    """An auto-save output error without mindroom_output_path releases claims immediately and leaves jobs unconsumed."""
    case = output_agent
    large_id = await case.save_payload_job(
        "f" * 64,
        ToolResultPayload(
            value=_LARGE_RESULT,
            state_delta=session_state_delta({"k": "before"}, {"k": "after"}),
        ),
        summary="large",
    )
    small_id = await case.save_payload_job(
        "1" * 64,
        ToolResultPayload(value="small-ok"),
        summary="small",
    )
    # Block auto-save directory creation with a regular file so auto-save returns a real output error receipt.
    (case.workspace / "mindroom_tool_outputs").write_text("blocker", encoding="utf-8")

    turn, state_after, _ = await case.owned_turn(
        calls(tool_call("job", "collect-auto-err", action="collect")),
        calls(tool_call("job", "wait-small", action="wait", job_id=small_id)),
        answer("recovered"),
        session_state={"k": "before"},
        run_id="collect-auto-err-run",
    )
    assert len(turn.tools or ()) == 2
    receipt = literal_eval(str(turn.tools[0].result))["mindroom_tool_output"]
    assert receipt["status"] == "error"
    # Immediate release let the same-turn wait claim and consume the small job while large_id stays unconsumed.
    assert str(turn.tools[1].result) == "small-ok"
    assert state_after == {"k": "before"}
    assert (await lookup(case.runtime, small_id, owner=case.owner, depth=0)).consumed
    assert not (await lookup(case.runtime, large_id, owner=case.owner, depth=0)).consumed
    reclaimed = await case.runtime.wait(large_id, owner=case.owner, depth=0, timeout=0)
    assert reclaimed.claim is not None
    await case.runtime.release_wait(large_id, reclaimed.claim)


@pytest.mark.parametrize("has_delta", [False, True])
async def test_wait_output_redirect_preserves_concurrent_sync_sibling_session_state(
    output_agent: _OutputAgent,
    monkeypatch: pytest.MonkeyPatch,
    has_delta: bool,
) -> None:
    """Committing a redirected wait never clears or clobbers session_state keys written by a concurrent sync sibling."""
    case = output_agent
    state_delta = (
        session_state_delta(
            {"updated": 1, "removed": "bye", "raced": "before"},
            {"updated": 2, "added": "new", "raced": "from-job"},
        )
        if has_delta
        else {}
    )
    job_id = await case.save_payload_job(
        "2" * 64,
        ToolResultPayload(value="redirected-report", state_delta=state_delta),
        summary="redirected-report",
    )
    sibling_started = threading.Event()
    finalize_entered = threading.Event()
    sibling_wrote = threading.Event()
    original_read = consumption_module.read_result_payload
    original_finalize = job_module.finalize_tool_output_file

    def sync_sibling() -> str:
        sibling_started.set()
        assert finalize_entered.wait(timeout=5)
        current_call().session_state["sibling_only"] = "from-sync-thread"
        if has_delta:
            current_call().session_state["raced"] = "from-sync-thread"
        sibling_wrote.set()
        return "sibling-ok"

    async def wait_for_sibling_read(runtime: ToolJobRuntime, job: BackgroundJob) -> ToolResultPayload:
        assert await asyncio.to_thread(sibling_started.wait, 5)
        return await original_read(runtime, job)

    def gated_finalize(request: ToolOutputFileRequest, result: object) -> object:
        finalize_entered.set()
        assert sibling_wrote.wait(timeout=5)
        return original_finalize(request, result)

    monkeypatch.setattr(consumption_module, "read_result_payload", wait_for_sibling_read)
    monkeypatch.setattr(job_module, "finalize_tool_output_file", gated_finalize)

    initial_state: dict[str, object] = (
        {"existing": "keep", "updated": 1, "removed": "bye", "raced": "before"} if has_delta else {"existing": "keep"}
    )
    turn, state_after, _ = await case.owned_turn(
        calls(
            tool_call("job", "wait-redirect", action="wait", job_id=job_id, mindroom_output_path="results/wait.txt"),
            tool_call("sync_sibling", "sibling-call"),
        ),
        answer("saved"),
        session_state=initial_state,
        run_id=f"wait-sync-sibling-{has_delta}",
        extra_toolkits=(adapt_toolkit(Toolkit(name="sync_sibling", tools=[sync_sibling])),),
    )
    wait_tool = next(tool for tool in turn.tools or () if tool.tool_call_id == "wait-redirect")
    receipt = literal_eval(str(wait_tool.result))["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    assert (case.workspace / receipt["path"]).read_text(encoding="utf-8") == "redirected-report"
    expected_state: dict[str, object] = (
        {
            "existing": "keep",
            "updated": 2,
            "added": "new",
            "raced": "from-sync-thread",
            "sibling_only": "from-sync-thread",
        }
        if has_delta
        else {"existing": "keep", "sibling_only": "from-sync-thread"}
    )
    assert state_after == expected_state
    assert (await lookup(case.runtime, job_id, owner=case.owner, depth=0)).consumed


async def test_collect_output_redirect_coalesces_chained_deltas_and_preserves_sync_sibling(
    output_agent: _OutputAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirected collect coalesces chained job deltas against live baselines without clobbering sync sibling writes."""
    case = output_agent
    first_id = await case.save_payload_job(
        "1" * 64,
        ToolResultPayload(
            value="first-report",
            state_delta=session_state_delta({"chain": 1, "raced_chain": 1}, {"chain": 2, "raced_chain": 2}),
        ),
        summary="first",
    )
    second_id = await case.save_payload_job(
        "2" * 64,
        ToolResultPayload(
            value="second-report",
            state_delta=session_state_delta({"chain": 2, "raced_chain": 2}, {"chain": 3, "raced_chain": 3}),
        ),
        summary="second",
    )
    sibling_started = threading.Event()
    finalize_entered = threading.Event()
    sibling_wrote = threading.Event()
    original_read = consumption_module.read_result_payload
    original_finalize = job_module.finalize_tool_output_file

    def sync_sibling() -> str:
        sibling_started.set()
        assert finalize_entered.wait(timeout=5)
        current_call().session_state["sibling_only"] = "from-sync-thread"
        # Writing the intermediate value must not match the second job's delta against the original baseline.
        current_call().session_state["raced_chain"] = 2
        sibling_wrote.set()
        return "sibling-ok"

    async def wait_for_sibling_read(runtime: ToolJobRuntime, job: BackgroundJob) -> ToolResultPayload:
        assert await asyncio.to_thread(sibling_started.wait, 5)
        return await original_read(runtime, job)

    def gated_finalize(request: ToolOutputFileRequest, result: object) -> object:
        finalize_entered.set()
        assert sibling_wrote.wait(timeout=5)
        return original_finalize(request, result)

    monkeypatch.setattr(consumption_module, "read_result_payload", wait_for_sibling_read)
    monkeypatch.setattr(job_module, "finalize_tool_output_file", gated_finalize)

    turn, state_after, _ = await case.owned_turn(
        calls(
            tool_call("job", "collect-redirect", action="collect", mindroom_output_path="results/collect.txt"),
            tool_call("sync_sibling", "sibling-call"),
        ),
        answer("saved"),
        session_state={"existing": "keep", "chain": 1, "raced_chain": 1},
        run_id="collect-sync-sibling",
        extra_toolkits=(adapt_toolkit(Toolkit(name="sync_sibling", tools=[sync_sibling])),),
    )
    collect_tool = next(tool for tool in turn.tools or () if tool.tool_call_id == "collect-redirect")
    receipt = literal_eval(str(collect_tool.result))["mindroom_tool_output"]
    assert receipt["status"] == "saved_to_file"
    saved_text = (case.workspace / receipt["path"]).read_text(encoding="utf-8")
    assert "first-report" in saved_text
    assert "second-report" in saved_text
    assert state_after == {
        "existing": "keep",
        "chain": 3,
        "raced_chain": 2,
        "sibling_only": "from-sync-thread",
    }
    assert (await lookup(case.runtime, first_id, owner=case.owner, depth=0)).consumed
    assert (await lookup(case.runtime, second_id, owner=case.owner, depth=0)).consumed


@pytest.mark.parametrize(("max_bytes", "committed"), [(None, True), (16, False)], ids=["commits", "releases"])
async def test_failed_managed_shell_job_wait_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    max_bytes: int | None,
    committed: bool,
) -> None:
    """Redirected wait on a failed managed shell job commits on save, or releases for plain-wait retry past max_bytes."""
    async with _output_agent(tmp_path, monkeypatch, tools=("shell",), max_bytes=max_bytes) as case:

        async def turn(cid: str, fn: str, **kw: object) -> RunRecord:
            rec, _, _ = await case.owned_turn(calls(tool_call(fn, cid, **kw)), answer("ok"), run_id=cid)
            return rec

        cmd = ["sh", "-c", "printf 'output longer than sixteen bytes\n' >&2; exit 7"]
        start = await turn("bg-start", "run_shell_command", args=cmd, background_after=0)
        assert start.tools
        job_id = json.loads(str(start.tools[0].result))["job_id"]
        ready = await case.runtime.wait(job_id, owner=case.owner, depth=0)
        assert ready.job.status == "failed"
        await case.runtime.release_wait(job_id, ready.claim)

        wait_rec = await turn("bg-wait", "job", action="wait", job_id=job_id, mindroom_output_path="build.log")
        assert wait_rec.tools
        assert (wait_rec.tools[0].is_error, not wait_rec.failing_tools) == (True, True)
        receipt = literal_eval(str(wait_rec.tools[0].result))["mindroom_tool_output"]
        assert (await lookup(case.runtime, job_id, owner=case.owner, depth=0)).consumed is committed
        if committed:
            saved_text = (case.workspace / "build.log").read_text(encoding="utf-8")
            assert (receipt["status"], receipt["path"]) == ("saved_to_file", "build.log")
            assert "output longer than sixteen bytes" in saved_text
            assert "7" in saved_text
            return
        assert (receipt["status"], (case.workspace / "build.log").exists()) == ("error", False)
        assert "16 byte limit" in receipt["error"]
        retry = await turn("bg-retry", "job", action="wait", job_id=job_id)
        assert retry.tools
        assert retry.tools[0].is_error is True
        assert "output longer than sixteen bytes" in str(retry.tools[0].result)
        assert (await lookup(case.runtime, job_id, owner=case.owner, depth=0)).consumed

        large = ToolResultPayload(None, error=_LARGE_RESULT, counts_as_tool_failure=False)
        large_id = await case.save_payload_job("9" * 64, large, "big", status="failed", tool_name="run_shell_command")
        auto = await turn("bg-auto", "job", action="wait", job_id=large_id)
        assert auto.tools
        auto_r = literal_eval(str(auto.tools[0].result))["mindroom_tool_output"]
        assert (auto.tools[0].is_error, not auto.failing_tools, auto_r["status"]) == (True, True, "error")
        assert "16 byte auto-save limit" in auto_r["error"]
        assert not (await lookup(case.runtime, large_id, owner=case.owner, depth=0)).consumed
