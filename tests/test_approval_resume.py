"""Production approval resumes continue the C6 pre-resume dumps into the rows Agno stored, and refuse what they must.

Each test loads a paused run from its pre-resume dump (``tests.approval_resume_helpers``) and continues it through
``AgentApprovalExecution`` with the decisions the capture took. The stored rows are compared with the ones Agno wrote
after the same resume; ``tests/test_ai_run_metadata_goldens.py`` compares the run metadata each resume publishes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.delegation.state import DelegationState, child_execution_identity
from mindroom.event_journal import approval_arguments_digest
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.response_turn import CompletedApprovalRun
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage
from tests.approval_continuation_helpers import receipt_call_ids
from tests.approval_resume_helpers import AGENT_RESUMES, ROOM, CodeResume, resume_case, setup_case, setup_resume
from tests.team_runtime.squad import comparable, golden_run, scripted

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

TIMED = ["duration", "end_time", "start_time"]


def _timed_like_the_owned_loop(expected: dict[str, Any]) -> dict[str, Any]:
    """Agno recorded no timing for the call it ran on resume; the owned loop times every call it runs."""
    for tool in expected["tools"]:
        if tool["metrics"] is None and tool.get("result") is not None:
            tool["metrics"] = TIMED
    for requirement in expected.get("requirements") or ():
        if requirement["tool_execution"]["metrics"] is None and requirement["tool_execution"].get("result") is not None:
            requirement["tool_execution"]["metrics"] = TIMED
    return expected


@pytest.mark.parametrize("run_id", ["run-approved", "run-rejected"])
async def test_a_paused_approval_resumes_into_the_rows_agno_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """Approving or denying the paused ``multiply`` continues the run exactly as Agno's continuation did."""
    case = AGENT_RESUMES[run_id]
    run = await setup_case(case, tmp_path, monkeypatch)
    before = run.paused.to_dict()

    result = await resume_case(case, run)

    assert isinstance(result, CompletedApprovalRun)
    assert run.executed == ([(6, 7)] if case.approve else [])
    expected = comparable(golden_run("agent_code", create_session_id(ROOM, case.thread), run_id))
    assert comparable(run.stored().to_dict()) == _timed_like_the_owned_loop(expected)
    assert run.paused.to_dict() == before
    # The resumed request carries the stored prompt, the turn, the decided call's result, and the receipt once.
    [request] = run.code.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert receipt_call_ids(request) == [f"call-{run_id}"]
    assert request.messages[-1].tool_call_id == f"call-{run_id}"


async def test_a_paused_delegation_resumes_its_child_and_then_the_parent_as_agno_did(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's approved call runs in the child, whose answer continues the parent as an external result."""
    case = AGENT_RESUMES["run-delegation-approved"]
    run = await setup_case(case, tmp_path, monkeypatch)
    [child] = DelegationState.from_metadata(run.paused.metadata).children
    assert [(call.invoking_agent, call.tool_name, call.toolkit_name) for call in run.calls()] == [
        ("helper", "multiply", "calculator"),
    ]

    result = await resume_case(case, run)

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
    [request] = run.code.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool"]
    assert request.messages[-1].tool_call_id == "call-delegate-approved"
    assert str(request.messages[-1].content).startswith("42")


def _save(run: CodeResume, record: RunRecord) -> None:
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
    run = await setup_resume(tmp_path, monkeypatch, thread="$approved", run_id="run-approved", code=code)
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
    run = await setup_resume(tmp_path, monkeypatch, thread="$approved", run_id="run-approved", code=code)
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
