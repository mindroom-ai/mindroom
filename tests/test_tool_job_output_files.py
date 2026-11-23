"""Workspace output policies cover managed result retrieval."""

from __future__ import annotations

from ast import literal_eval
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
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
from mindroom.tool_jobs.consumption import session_state_delta, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, read_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolValue
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

    async def save_payload_job(self, job_id: str, payload: ToolResultPayload, summary: str = "done") -> str:
        async def operation() -> BackgroundOutcome:
            return BackgroundOutcome("completed", summary, result_payload=encode_result_payload(payload))

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

    async def owned_turn(
        self,
        *steps: Sequence[ScriptStep],
        session_state: dict[str, object] | None = None,
        run_id: str = "owned-run",
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
                    replace(self.build.spec, system_messages=prompt_for_turn(self.build)),
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


@pytest_asyncio.fixture
async def output_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_OutputAgent]:
    """Build a workspace agent with the owned loop's dispatch and isolated durable jobs."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])},
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
