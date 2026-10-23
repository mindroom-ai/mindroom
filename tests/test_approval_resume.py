"""Production approval resumes continue the C6 pre-resume dumps into the rows Agno stored, and refuse what they must.

Each test loads the sessions one approval resume read, as the capture dumped them just before the resume, into the
stores the ``code`` and ``helper`` agents use, and continues the paused run through ``AgentApprovalExecution`` with
the decisions the capture took. The stored rows are compared with the ones Agno wrote after the same resume.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.approval.receipt import build_approval_receipt
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.config.main import Config
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.delegation import records
from mindroom.delegation.audit import start_child_record
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalDecision, approval_arguments_digest
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.response_turn import CompletedApprovalRun, paused_attempt_from_response
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.delegation_helpers import ScriptedAgents, approval_calls_for, approval_executor, resume
from tests.fakes.scripted_model import answer, usage
from tests.team_runtime.squad import STORAGE, comparable, golden_run, scripted

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.response_turn import PausedAttempt
    from mindroom.session_storage.store import ConversationStore
    from tests.fakes.scripted_model import ScriptedModel

pytestmark = pytest.mark.asyncio

ROOM = "!room:example.org"
REQUESTER = "@alice:example.org"
TIMED = ["duration", "end_time", "start_time"]
# The capture's agents and models, as far as the ``code`` agent's resumes reach them.
CONFIG: dict[str, Any] = {
    "timezone": "UTC",
    "defaults": {"tools": []},
    "memory": {"backend": "none"},
    "agents": {
        "code": {
            "display_name": "Code",
            "role": "Writes and checks code.",
            "model": "code",
            "tools": ["calculator", "attachments"],
            "delegate_to": ["helper"],
            "memory_backend": "file",
        },
        "helper": {
            "display_name": "Helper",
            "role": "Runs calculations for other agents.",
            "model": "helper",
            "tools": ["calculator"],
            "access": {"users": [REQUESTER]},
        },
    },
    "models": {
        "default": {"provider": "synthetic", "id": "synthetic"},
        "code": {"provider": "synthetic", "id": "code-model", "display_name": "Code Model", "context_window": 200000},
        "helper": {"provider": "synthetic", "id": "helper-model"},
    },
    "tool_approval": {"default": "auto_approve", "rules": [{"match": "multiply", "action": "require_approval"}]},
}


@dataclass
class _Resume:
    """One paused ``code`` run loaded from its pre-resume dump, with the stores and models its resume reaches."""

    config: Config
    paths: RuntimePaths
    identity: ToolExecutionIdentity
    store: ConversationStore
    paused: RunRecord
    attempt: PausedAttempt
    code: ScriptedModel
    helper: ScriptedModel
    executed: list[tuple[float, float]]

    def stored(self) -> RunRecord:
        record = self.store.get_run(self.paused.run_id)
        assert isinstance(record, RunRecord)
        return record

    def calls(self) -> tuple[ApprovalCall, ...]:
        """Return the approval cards' calls for the pause as the reply published them."""
        return approval_calls_for(self.attempt, default_agent="code", toolkit_names={"multiply": "calculator"})

    async def resume(self, calls: tuple[ApprovalCall, ...], *, approve: bool, prompt: str) -> object:
        decision = ApprovalDecision.APPROVED if approve else ApprovalDecision.DENIED
        reason = None if approve else "Alice declined the multiplication."
        decided = tuple(replace(call, decision=decision, reason=reason) for call in calls)
        with approval_receipt_context(build_approval_receipt(decided), run_id=self.paused.run_id):
            return await resume(
                approval_executor(self.config, self.paths),
                self.attempt,
                calls,
                agent_name="code",
                identity=self.identity,
                config=self.config,
                runtime_paths=self.paths,
                decisions={call.tool_call_id: approve for call in calls},
                denial_reasons={call.tool_call_id: reason for call in calls},
                approval_id=f"approval-{self.paused.run_id}",
                request_body=prompt,
            )


def _load_dump(store: ConversationStore, dump: str) -> None:
    """Write one pre-resume dump into ``store``'s database, as the resume found it."""
    store.path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(store.path)) as connection:
        connection.executescript((STORAGE / f"{dump}.sql").read_text(encoding="utf-8"))


async def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread: str,
    run_id: str,
    code: ScriptedModel,
    helper: ScriptedModel | None = None,
) -> _Resume:
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(CONFIG), paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=REQUESTER,
        room_id=ROOM,
        thread_id=thread,
        resolved_thread_id=thread,
        session_id=create_session_id(ROOM, thread),
    )
    store = create_session_storage("code", config, paths, identity)
    _load_dump(store, f"agent_code__before_{run_id}")
    paused = store.get_run(run_id)
    assert isinstance(paused, RunRecord)
    assert paused.status is RunStatus.paused
    for child in DelegationState.from_metadata(paused.metadata).children:
        child_store = create_session_storage("helper", config, paths, child_execution_identity(child))
        _load_dump(child_store, f"agent_helper__before_{run_id}")
        # The child's audit record is on disk beside its store; the capture started it on its frozen clock.
        monkeypatch.setattr(records, "_utc_timestamp", lambda: "2027-01-15T08:26:00+00:00")
        await start_child_record(
            child,
            parent_run_id=run_id,
            config=config,
            runtime_paths=paths,
            caller_execution_identity=identity,
        )
    helper = helper or scripted("helper-model")
    ScriptedAgents({"code": code, "helper": helper}).install(monkeypatch)
    executed: list[tuple[float, float]] = []
    original_multiply = CalculatorTools.multiply

    def multiply(self: CalculatorTools, a: float, b: float) -> str:
        executed.append((a, b))
        return original_multiply(self, a, b)

    monkeypatch.setattr(CalculatorTools, "multiply", multiply)
    attempt = paused_attempt_from_response(
        paused,
        fallback_session_id=paused.session_id,
        fallback_run_id=paused.run_id,
        toolkit_owners={("code", "multiply"): "calculator"},
    )
    assert attempt is not None
    return _Resume(config, paths, identity, store, paused, attempt, code, helper, executed)


def _timed_like_the_owned_loop(expected: dict[str, Any]) -> dict[str, Any]:
    """Agno recorded no timing for the call it ran on resume; the owned loop times every call it runs."""
    for tool in expected["tools"]:
        if tool["metrics"] is None and tool.get("result") is not None:
            tool["metrics"] = TIMED
    for requirement in expected.get("requirements") or ():
        if requirement["tool_execution"]["metrics"] is None and requirement["tool_execution"].get("result") is not None:
            requirement["tool_execution"]["metrics"] = TIMED
    return expected


@pytest.mark.parametrize(
    ("thread", "run_id", "approve", "reply"),
    [
        ("$approved", "run-approved", True, "6 x 7 = 42."),
        ("$rejected", "run-rejected", False, "I did not multiply, because the call was declined."),
    ],
)
async def test_a_paused_approval_resumes_into_the_rows_agno_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    thread: str,
    run_id: str,
    reply: str,
    *,
    approve: bool,
) -> None:
    """Approving or denying the paused ``multiply`` continues the run exactly as Agno's continuation did."""
    code = scripted("code-model", answer(reply, usage(260, 9, time_to_first_token=0.25)))
    run = await _setup(tmp_path, monkeypatch, thread=thread, run_id=run_id, code=code)
    before = run.paused.to_dict()

    result = await run.resume(run.calls(), approve=approve, prompt="Multiply 6 by 7.")

    assert isinstance(result, CompletedApprovalRun)
    assert run.executed == ([(6, 7)] if approve else [])
    expected = comparable(golden_run("agent_code", create_session_id(ROOM, thread), run_id))
    assert comparable(run.stored().to_dict()) == _timed_like_the_owned_loop(expected)
    assert run.paused.to_dict() == before
    # The resumed request carries the stored prompt, the turn, the decided call's result, and the receipt once.
    [request] = code.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert request.messages[0].provider_data == {"mindroom_approval_receipt": True}
    assert request.messages[-1].tool_call_id == f"call-{run_id}"


async def test_a_paused_delegation_resumes_its_child_and_then_the_parent_as_agno_did(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's approved call runs in the child, whose answer continues the parent as an external result."""
    code = scripted("code-model", answer("The helper found 42.", usage(330, 6, time_to_first_token=0.25)))
    helper = scripted("helper-model", answer("42", usage(100, 1, time_to_first_token=0.25)))
    run = await _setup(
        tmp_path,
        monkeypatch,
        thread="$delegation-approved",
        run_id="run-delegation-approved",
        code=code,
        helper=helper,
    )
    [child] = DelegationState.from_metadata(run.paused.metadata).children
    calls = run.calls()
    assert [(call.invoking_agent, call.tool_name, call.toolkit_name) for call in calls] == [
        ("helper", "multiply", "calculator"),
    ]

    result = await run.resume(calls, approve=True, prompt="Ask the helper to multiply 6 by 7.")

    assert isinstance(result, CompletedApprovalRun)
    assert run.executed == [(6, 7)]
    session_id = create_session_id(ROOM, "$delegation-approved")
    expected = comparable(golden_run("agent_code", session_id, "run-delegation-approved"))
    assert comparable(run.stored().to_dict()) == expected
    child_store = create_session_storage("helper", run.config, run.paths, child_execution_identity(child))
    child_record = child_store.get_run(child.run_id)
    assert isinstance(child_record, RunRecord)
    expected_child = comparable(golden_run("agent_helper", child.session_id, child.run_id))
    assert comparable(child_record.to_dict()) == _timed_like_the_owned_loop(expected_child)
    # The parent reads the child's answer where it read the pause, on its own stored request.
    [request] = code.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert request.messages[-1].tool_call_id == "call-delegate-approved"
    assert str(request.messages[-1].content).startswith("42")


def _save(run: _Resume, record: RunRecord) -> None:
    session = run.store.load_session(record.session_id or "", "agent")
    assert session is not None
    run.store.save_runs(session, [record])


@pytest.mark.parametrize("tampering", ["planted_confirmed", "mismatched_digest", "unknown_id", "outstanding_external"])
async def test_a_resume_refuses_tampered_or_incomplete_decisions_before_anything_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampering: str,
) -> None:
    """Exact coverage of the waiting calls, checked digests, and no planted or unresolved entries, or nothing runs.

    ``planted_confirmed`` adds a second gated call already marked confirmed to the stored row; ``mismatched_digest``
    approves the real call under other arguments; ``unknown_id`` approves a call the run never made; and
    ``outstanding_external`` leaves the stored row also waiting on an externally executed call that is not a
    delegation, which no decision resolves.
    """
    code = scripted("code-model")  # Any request would fail: the scripted model has no turn for it.
    run = await _setup(tmp_path, monkeypatch, thread="$approved", run_id="run-approved", code=code)
    [call] = run.calls()
    if tampering in {"planted_confirmed", "outstanding_external"}:
        tampered = deepcopy(run.paused)
        extra = ToolExecution(
            tool_call_id="call-planted",
            tool_name="multiply" if tampering == "planted_confirmed" else "fetch_remote",
            tool_args={"a": 6, "b": 8} if tampering == "planted_confirmed" else {"url": "https://example.org"},
            requires_confirmation=tampering == "planted_confirmed",
            confirmed=True if tampering == "planted_confirmed" else None,
            external_execution_required=tampering == "outstanding_external",
        )
        tampered.tools = [*(tampered.tools or ()), extra]
        tampered.requirements = [*(tampered.requirements or ()), RunRequirement(deepcopy(extra))]
        _save(run, tampered)
        run.paused = run.stored()
    elif tampering == "mismatched_digest":
        call = replace(call, arguments_digest=approval_arguments_digest({"a": 6, "b": 8}))
    else:
        call = replace(call, tool_call_id="call-unknown")
    before = run.stored().to_dict()

    with pytest.raises((RuntimeError, ValueError), match="retry the request"):
        await run.resume((call,), approve=True, prompt="Multiply 6 by 7.")

    assert run.executed == []
    assert code.requests == []
    assert run.stored().to_dict() == before
    assert run.stored().status is RunStatus.paused


async def test_a_waiting_call_marked_confirmed_in_the_store_is_refused_even_when_the_requester_approves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stored row whose waiting call someone marked confirmed is refused, even with the requester's approval."""
    code = scripted("code-model")
    run = await _setup(tmp_path, monkeypatch, thread="$approved", run_id="run-approved", code=code)
    [call] = run.calls()
    tampered = deepcopy(run.paused)
    [requirement] = tampered.requirements or ()
    requirement.tool_execution.confirmed = True
    [tool] = tampered.tools or ()
    tool.confirmed = True
    _save(run, tampered)
    run.paused = run.stored()

    with pytest.raises((RuntimeError, ValueError), match="retry the request"):
        await run.resume((call,), approve=True, prompt="Multiply 6 by 7.")

    assert run.executed == []
    assert code.requests == []
    assert run.stored().status is RunStatus.paused
