"""Run every numbered example workflow from the spec on LocalWorkflowStack with a scripted model."""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.api.listener import serve_listener
from mindroom.config.models import ModelConfig
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.answers import answer_question
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    WorkflowNode,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
)
from tests.conftest import bound_worker_launch_config
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.scripted_agent_helpers import raising
from tests.test_workflow_local_integration import LocalWorkflowStack
from tests.workflow_helpers import workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.llm.provider import ModelRequest, StreamEvent

_EXAMPLES_DIR = Path("tests/workflow_examples")
_SPEC_PATH = Path("docs/baspowers/specs/2026-11-19-agent-graphs-design.md")
_ROOM = "!room:example.test"
_THREAD = "$thread:example.test"
_ALICE = "@alice:example.test"

type _Scenario = tuple[dict[str, object], Callable[[ModelRequest], list[ScriptStep]], Callable[[object], None]]


class _RoutingModel(ScriptedModel):
    """Deterministic scripted model choosing turn steps from request prompts and tool outputs."""

    def __init__(self, handler: Callable[[ModelRequest], list[ScriptStep]]) -> None:
        super().__init__(turns=[])
        self._handler = handler

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.turns.append(self._handler(request))
        async for event in super().stream(request):
            yield event


def _unwrap_prompt(text: str) -> str:
    if "<![CDATA[" in text and "]]></msg>" in text:
        return text.split("<![CDATA[", 1)[1].rsplit("]]></msg>", 1)[0]
    return text


def _user_texts(request: ModelRequest) -> list[str]:
    return [_unwrap_prompt(str(m.content)) for m in request.messages if m.role == "user"]


def _tool_outputs(request: ModelRequest) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool" and isinstance(m.content, str)]


def _json_block(value: object) -> list[ScriptStep]:
    return answer(f"```json\n{json.dumps(value)}\n```")


@pytest_asyncio.fixture
async def local_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LocalWorkflowStack]:
    """Real subprocess SDK + real local HTTP gateway + scripted agents + room for question cards."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    with (
        workflow_stack(
            tmp_path,
            stack_type=LocalWorkflowStack,
            extra_tools=("workflow",),
            require_approval=(),
        ) as stack,
        bound_worker_launch_config(stack.context.config),
    ):
        assert isinstance(stack, LocalWorkflowStack)
        stack.context.config.models["haiku"] = ModelConfig(provider="openai", id="haiku-model")
        stack.context.config.models["opus"] = ModelConfig(provider="openai", id="opus-model")
        stack.show()
        stack.gateway_url = stack.scripts.gateway_url = f"http://127.0.0.1:{port}/api/script-gateway"
        stack.served = stack.app()
        stack.continues = coordinator_module._continues
        stack.engine.agents = stack._agents
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        async with serve_listener(
            stack.served,
            name="workflow_examples_gateway",
            host="127.0.0.1",
            port=port,
            log_level="WARNING",
            ws="none",
            access_log=False,
        ):
            yield stack
        await stack.close()


async def _wait_for(predicate: Callable[[], object], *, limit: float = 90.0) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110 - polling SQLite state of child process
            await asyncio.sleep(0.02)


async def _run_to_end(
    local_stack: LocalWorkflowStack,
    source: str,
    args: dict[str, object],
    *,
    max_concurrent: int = 16,
) -> WorkflowRun:
    run = await local_stack.start(source=source, args=args, max_concurrent=max_concurrent)
    await _wait_for(lambda: local_stack.store.get_run(run.run_id).state in TERMINAL_RUN_STATES)
    return local_stack.store.get_run(run.run_id)


def _diamond_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Split this question"):
            return _json_block(["durability", "concurrency", "failing-angle"])
        if prompt.startswith("Research: failing-angle"):
            return [raising(RuntimeError("search offline"))]
        if prompt.startswith("Research:"):
            angle = prompt.splitlines()[0].removeprefix("Research: ").strip()
            return answer(f"Notes on {angle}")
        assert "failing-angle" in prompt
        assert "Notes on durability" in prompt
        return answer("Synthesized report with missing noted")

    def check(result: object) -> None:
        assert result == "Synthesized report with missing noted"

    return {"question": "How does WAL work?"}, handle, check


def _audit_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Audit auth.py"):
            return _json_block([{"file": "auth.py", "line": 12, "claim": "missing check"}])
        if prompt.startswith("Audit util.py"):
            return _json_block([{"file": "util.py", "line": 4, "claim": "false alarm"}])
        if "auth.py" in prompt:
            return _json_block({"real": True, "reason": "verified bug"})
        return _json_block({"real": False, "reason": "guarded upstream"})

    def check(result: object) -> None:
        assert result == {
            "confirmed": [{"file": "auth.py", "line": 12, "claim": "missing check", "reason": "verified bug"}],
            "rejected": 1,
        }

    return {"files": ["auth.py", "util.py"], "issue": "auth bypass"}, handle, check


def _review_scenario() -> _Scenario:
    reviews = 0

    def handle(req: ModelRequest) -> list[ScriptStep]:
        nonlocal reviews
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Review `git diff main`"):
            reviews += 1
            return answer("APPROVE: clean diff" if reviews >= 2 else "Fix edge case")
        return answer("committed")

    def check(result: object) -> None:
        assert isinstance(result, str)
        assert result.startswith("Done in 2 round(s).")

    return {"worktree": ".", "task": "Add feature", "test_command": "true"}, handle, check


def _panel_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Write a plan"):
            label = "opus" if any("opus" in str(m.content) for m in req.messages if m.role == "system") else "haiku"
            return answer(f"Plan from {label}")
        if prompt.startswith("Score this plan"):
            score = 9 if "opus" in prompt else 6
            return _json_block({"score": score, "why": "solid"})
        return answer("Merged winning plan")

    def check(result: object) -> None:
        assert result == "Merged winning plan"

    return {"models": ["haiku", "opus"], "task": "Design cache"}, handle, check


def _loop_until_dry_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if "(none yet)" in prompt:
            area = prompt.split("Find bugs in ", 1)[1].split(".", 1)[0]
            return _json_block([{"id": f"bug-{area}", "text": f"issue in {area}"}])
        return _json_block([])

    def check(result: object) -> None:
        assert isinstance(result, list)
        assert {item["id"] for item in result} == {"bug-auth", "bug-billing"}

    return {"areas": ["auth", "billing"], "max_rounds": 4}, handle, check


def _debate_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Decide, and say why."):
            assert "Adopt SQLite" in prompt
            return answer("Verdict: adopt SQLite")
        return answer(f"Turn reply ({len(_user_texts(req))})")

    def check(result: object) -> None:
        assert result == "Verdict: adopt SQLite"

    return {"proposal": "Adopt SQLite"}, handle, check


def _team_backend_turn(outs: list[dict[str, Any]]) -> list[ScriptStep]:
    if len(outs) == 0:
        return calls(tool_call("send_peer_message", "b1", to="frontend", text="v1: {id, name}"))
    if len(outs) == 1:
        return calls(tool_call("read_peer_messages", "b2", wait_seconds=600))
    if len(outs) == 2:
        assert any("avatar" in m["text"] for m in outs[1]["messages"])
        return calls(tool_call("set_shared_state", "b3", key="contract", value={"fields": ["id", "name", "avatar"]}))
    return answer("API built")


def _team_frontend_turn(outs: list[dict[str, Any]]) -> list[ScriptStep]:
    if len(outs) == 0:
        return calls(tool_call("read_peer_messages", "f1", wait_seconds=600))
    if len(outs) == 1:
        assert any("v1" in m["text"] for m in outs[0]["messages"])
        return calls(tool_call("send_peer_message", "f2", to="backend", text="add avatar field"))
    return answer("UI built")


def _team_scenario() -> _Scenario:
    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[0]
        outs = _tool_outputs(req)
        if prompt.startswith("Build the API"):
            return _team_backend_turn(outs)
        if prompt.startswith("Build the UI"):
            return _team_frontend_turn(outs)
        assert "avatar" in prompt
        assert "API built" in prompt
        assert "UI built" in prompt
        return answer("Integration clean")

    def check(result: object) -> None:
        assert result == "Integration clean"

    return {"feature": "User profile"}, handle, check


_SCENARIOS: dict[str, Callable[[], _Scenario]] = {
    "01_diamond_research": _diamond_scenario,
    "02_audit_adversarial": _audit_scenario,
    "03_implement_review_fix": _review_scenario,
    "04_panel_and_judge": _panel_scenario,
    "05_loop_until_dry": _loop_until_dry_scenario,
    "06a_debate": _debate_scenario,
    "06b_team": _team_scenario,
}


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.parametrize("example", sorted(_EXAMPLES_DIR.glob("*.py")), ids=lambda p: p.stem)
@pytest.mark.asyncio
async def test_spec_example_runs(local_stack: LocalWorkflowStack, example: Path) -> None:
    """Every numbered example script from the spec completes cleanly on LocalWorkflowStack."""
    args, handler, check = _SCENARIOS[example.stem]()
    local_stack.models["watcher"] = _RoutingModel(handler)
    ended = await _run_to_end(local_stack, example.read_text(encoding="utf-8"), args)
    assert ended.state is WorkflowRunState.COMPLETED, ended.error
    check(ended.result)


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
async def test_loop_until_dry_fails_when_a_finder_fails(local_stack: LocalWorkflowStack) -> None:
    """In example 5, a failing finder fails the run instead of falsely reporting no bugs."""
    source = (_EXAMPLES_DIR / "05_loop_until_dry.py").read_text(encoding="utf-8")

    def handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        if "Find bugs in billing" in prompt:
            return [raising(RuntimeError("finder crashed"))]
        return _json_block([])

    local_stack.models["watcher"] = _RoutingModel(handle)
    ended = await _run_to_end(local_stack, source, {"areas": ["auth", "billing"], "max_rounds": 3})
    assert ended.state is WorkflowRunState.FAILED
    assert ended.error is not None
    assert "PartialFailure" in ended.error


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
async def test_review_loop_runs_five_more_rounds_when_asked(local_stack: LocalWorkflowStack) -> None:
    """In example 3, answering 'Five more rounds' at round 5 runs rounds 6..7 until approval."""
    source = (_EXAMPLES_DIR / "03_implement_review_fix.py").read_text(encoding="utf-8")
    reviews = 0

    def handle(req: ModelRequest) -> list[ScriptStep]:
        nonlocal reviews
        prompt = _user_texts(req)[-1]
        if prompt.startswith("Review `git diff main`"):
            reviews += 1
            return answer("APPROVE: converged on round 7" if reviews >= 7 else f"Issue in round {reviews}")
        return answer("fixed")

    local_stack.models["watcher"] = _RoutingModel(handle)
    run = await local_stack.start(
        source=source,
        args={"worktree": ".", "task": "Add feature", "test_command": "true"},
    )

    def open_ask() -> WorkflowNode | None:
        return next(
            (
                n
                for n in local_stack.store.nodes(run.run_id)
                if n.kind == "ask" and n.state is WorkflowNodeState.WAITING and "event_id" in n.links
            ),
            None,
        )

    await _wait_for(open_ask)
    node = open_ask()
    assert node is not None
    outcome = await answer_question(
        local_stack.engine,
        run_id=run.run_id,
        node_id=f"n{node.seq}",
        sender=_ALICE,
        room_id=_ROOM,
        thread_id=_THREAD,
        card_event_id=node.links["event_id"],
        option="Five more rounds",
        text=None,
    )
    assert outcome == "accepted"

    await _wait_for(lambda: local_stack.store.get_run(run.run_id).state in TERMINAL_RUN_STATES)
    ended = local_stack.store.get_run(run.run_id)
    assert ended.state is WorkflowRunState.COMPLETED, ended.error
    assert isinstance(ended.result, str)
    assert ended.result.startswith("Done in 7 round(s).")


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
async def test_team_example_finishes_at_max_concurrent_one(local_stack: LocalWorkflowStack) -> None:
    """Example 6b (direct peer negotiation + shared state) finishes cleanly even at max_concurrent=1."""
    source = (_EXAMPLES_DIR / "06b_team.py").read_text(encoding="utf-8")
    args, handler, check = _team_scenario()
    local_stack.models["watcher"] = _RoutingModel(handler)
    ended = await _run_to_end(local_stack, source, args, max_concurrent=1)
    assert ended.state is WorkflowRunState.COMPLETED, ended.error
    check(ended.result)


def test_examples_in_the_spec_and_in_tests_are_the_same_text() -> None:
    """The numbered fenced Python blocks under `## Example workflows` in the spec match tests/workflow_examples/*.py."""
    spec = _SPEC_PATH.read_text(encoding="utf-8")
    section = spec.split("## Example workflows\n", 1)[1].split("\n## ", 1)[0]
    numbered = section.split("\n### Agents that edit", 1)[0]
    spec_blocks = re.findall(r"```python\n(.*?)```", numbered, re.DOTALL)
    files = sorted(_EXAMPLES_DIR.glob("*.py"))
    assert len(files) == len(spec_blocks) == 7
    for path, expected in zip(files, spec_blocks, strict=True):
        assert path.read_text(encoding="utf-8") == expected, f"{path} drifted from spec"
