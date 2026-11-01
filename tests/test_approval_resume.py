"""Production approval resumes continue runs Agno stored paused into completed runs, and refuse what they must.

Each test loads a paused run from its pre-resume dump (``tests.approval_resume_helpers``), so a run paused before the
upgrade resumes after it, and continues it through ``AgentApprovalExecution`` with the decisions the capture took.
The stored records are asserted as typed values; ``tests/test_ai_run_metadata_goldens.py`` compares the run metadata
each resume publishes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState, child_execution_identity
from mindroom.event_journal import approval_arguments_digest
from mindroom.llm.provider import TurnCompleted
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.response_turn import CompletedApprovalRun
from mindroom.session_storage.agent_storage import create_session_storage
from tests.approval_continuation_helpers import receipt_call_ids
from tests.approval_resume_helpers import AGENT_RESUMES, CodeResume, resume_case, setup_case, setup_resume
from tests.run_record_helpers import (
    PRODUCT,
    approved_call,
    as_stored,
    denied_call,
    result,
    resumed,
    settled,
    waiting_call,
)
from tests.teams.squad import scripted

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.llm.usage import MessageMetrics
    from tests.fakes.scripted_model import ScriptStep

pytestmark = pytest.mark.asyncio


def _answered(script: Sequence[ScriptStep]) -> tuple[str, MessageMetrics]:
    """Return the text and usage of the reply a scripted model answers with."""
    [step] = [step for step in script if isinstance(step, TurnCompleted)]
    assert isinstance(step.message.content, str)
    return step.message.content, step.message.metrics


def _without_published_metadata(record: RunRecord, *keys: str) -> dict[str, object]:
    """Return ``record``'s run metadata without ``keys`` and what the reply publishes, which the metadata goldens pin."""
    left_out = {AI_RUN_METADATA_KEY, *keys}
    return {key: value for key, value in (record.metadata or {}).items() if key not in left_out}


@pytest.mark.parametrize("run_id", ["run-approved", "run-rejected"])
async def test_a_paused_approval_resumes_into_a_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """Approving the paused ``multiply`` runs it, denying it answers it with the reason, and the run completes."""
    case = AGENT_RESUMES[run_id]
    run = await setup_case(case, tmp_path, monkeypatch)
    before = run.paused.to_dict()

    outcome = await resume_case(case, run)

    assert isinstance(outcome, CompletedApprovalRun)
    assert run.executed == ([(6, 7)] if case.approve else [])
    # The run stores the receipt its resumed request sent where it sent it, after the decided call's result.
    [request] = run.code.requests
    receipt = request.messages[-1]
    call, waiting = waiting_call(run.paused)
    if case.approve:
        expected = resumed(
            run.paused,
            decided=approved_call(waiting, PRODUCT),
            added=[result(call, PRODUCT), receipt],
            answered=_answered(case.code),
        )
    else:
        reason = "Alice declined the multiplication."
        expected = resumed(
            run.paused,
            decided=denied_call(waiting, reason),
            added=[result(call, reason, is_error=True), receipt],
            answered=_answered(case.code),
        )
    expected = as_stored(expected)
    stored = run.stored()
    assert settled(replace(stored, metadata=None)) == settled(replace(expected, metadata=None))
    assert _without_published_metadata(stored) == _without_published_metadata(run.paused)
    assert run.paused.to_dict() == before
    # The resumed request carries the stored prompt, the turn, the decided call's result, then the receipt once.
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool", "user"]
    assert receipt_call_ids(request) == [f"call-{run_id}"]
    assert request.messages[-2].tool_call_id == f"call-{run_id}"


async def test_a_paused_delegation_resumes_its_child_and_then_the_parent(
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
    child_store = create_session_storage("helper", run.config, run.paths, child_execution_identity(child))
    paused_child = child_store.get_run(child.run_id)
    assert isinstance(paused_child, RunRecord)

    outcome = await resume_case(case, run)

    assert isinstance(outcome, CompletedApprovalRun)
    assert run.executed == [(6, 7)]
    # The child runs its approved call and answers, as a resumed run does.
    child_call, child_waiting = waiting_call(paused_child)
    assert case.helper is not None
    child_record = child_store.get_run(child.run_id)
    assert isinstance(child_record, RunRecord)
    # Each resumed run stores the receipt its request sent, after the decided call's result.
    child_receipt = run.helper.requests[-1].messages[-1]
    assert receipt_call_ids(run.helper.requests[-1]) == [child_call["id"]]
    assert settled(child_record) == settled(
        as_stored(
            resumed(
                paused_child,
                decided=approved_call(child_waiting, PRODUCT),
                added=[result(child_call, PRODUCT), child_receipt],
                answered=_answered(case.helper),
            ),
        ),
    )
    # The parent's delegation is answered with the child's answer and its record, as an externally executed result.
    handed_over = (
        f"42\n\nDelegation {child.delegation_id} record: helper:.mindroom/delegations/2027-01-15/{child.delegation_id}"
        f"\nSubagent ID: {child.subagent_id}"
    )
    call, waiting = waiting_call(run.paused)
    [request] = run.code.requests
    expected = as_stored(
        resumed(
            run.paused,
            decided=replace(waiting, result=handed_over, external_execution_required=False),
            added=[result(call, handed_over, is_error=None), request.messages[-1]],
            answered=_answered(case.code),
        ),
    )
    stored = run.stored()
    assert settled(replace(stored, metadata=None)) == settled(replace(expected, metadata=None))
    # The parent's delegation state settles the child with its answer, ran its after-call hook, and waits on nothing;
    # the rest of its run metadata stays.
    paused_state = DelegationState.from_metadata(run.paused.metadata)
    assert DelegationState.from_metadata(stored.metadata) == replace(
        paused_state,
        children=[replace(child, status="completed", result="42")],
        hooks={call_id: replace(hook, after_called=True) for call_id, hook in paused_state.hooks.items()},
        pending_tools=[],
        pending_tool_sources={},
        pending_child_id=None,
        pending_agent_name=None,
    )
    assert _without_published_metadata(stored, DELEGATION_STATE_KEY) == _without_published_metadata(
        run.paused,
        DELEGATION_STATE_KEY,
    )
    # The parent reads the child's answer where it read the pause, on its own stored request, then the receipt.
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool", "user"]
    assert request.messages[-2].tool_call_id == "call-delegate-approved"
    assert str(request.messages[-2].content).startswith("42")
    # The parent holds no such call, so it reads the card's id for the child's approved call.
    assert receipt_call_ids(request) == [call.tool_call_id for call in run.calls()]


_RECEIPT = (
    "[SYSTEM NOTICE — TOOL APPROVAL RECEIPT] This trusted MindRoom runtime receipt records how paused tool calls "
    "were authorized. Do not infer approval policy from tool success alone.\n- `multiply` (tool call id `{call_id}`): "
    "{outcome}"
)
_GRANTED = "approval was granted, but its approval provenance is unavailable."
_PRODUCT_RESULT = '{"operation": "multiplication", "result": 42.0}'


@pytest.mark.parametrize("run_id", sorted(AGENT_RESUMES))
async def test_a_resume_sends_each_model_its_stored_request_then_its_decided_results_and_the_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    """Each model a resume reaches gets its paused run's stored messages, each decided call's result, then the receipt.

    An approved call answers with what it returned and a denied one with the requester's reason; the receipt names
    each decision under the id its reader knows the call by, and a delegated child's answer reaches the parent as the
    delegation's result.
    """
    case = AGENT_RESUMES[run_id]
    run = await setup_case(case, tmp_path, monkeypatch)
    stored = {"code-model": run.paused.messages}
    children = DelegationState.from_metadata(run.paused.metadata).children
    for child in children:
        child_store = create_session_storage("helper", run.config, run.paths, child_execution_identity(child))
        paused_child = child_store.get_run(child.run_id)
        assert isinstance(paused_child, RunRecord)
        stored["helper-model"] = paused_child.messages

    await resume_case(case, run)

    sent = {}
    for model in (run.code, run.helper):
        for request in model.requests:
            prefix = stored[model.identity.id]
            assert list(request.messages[: len(prefix)]) == prefix
            sent[model.identity.id] = [
                (message.role, message.tool_call_id, str(message.content), message.tool_call_error)
                for message in request.messages[len(prefix) :]
            ]
    receipt = partial(_RECEIPT.format, outcome=_GRANTED)
    if run_id == "run-approved":
        expected = {
            "code-model": [
                ("tool", "call-run-approved", _PRODUCT_RESULT, False),
                ("user", None, receipt(call_id="call-run-approved"), None),
            ],
        }
    elif run_id == "run-rejected":
        expected = {
            "code-model": [
                ("tool", "call-run-rejected", "Alice declined the multiplication.", True),
                (
                    "user",
                    None,
                    _RECEIPT.format(
                        call_id="call-run-rejected",
                        outcome="approval was denied; the tool was not executed.",
                    ),
                    None,
                ),
            ],
        }
    else:
        [child] = children
        handed_over = (
            f"42\n\nDelegation {child.delegation_id} record: helper:.mindroom/delegations/2027-01-15/"
            f"{child.delegation_id}\nSubagent ID: {child.subagent_id}"
        )
        expected = {
            "helper-model": [
                ("tool", "call-helper-multiply-approved", _PRODUCT_RESULT, False),
                ("user", None, receipt(call_id="call-helper-multiply-approved"), None),
            ],
            "code-model": [
                ("tool", "call-delegate-approved", handed_over, None),
                ("user", None, receipt(call_id=f"{child.delegation_id}:call-helper-multiply-approved"), None),
            ],
        }
    assert sent == expected


def _save(run: CodeResume, record: RunRecord) -> None:
    session = run.store.load_session(record.session_id or "")
    assert session is not None
    run.store.save_run(session, record, record_usage=False)


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
    [tool] = tampered.tools or ()
    tool.confirmed = True
    _save(run, tampered)
    run.paused = run.stored()

    with pytest.raises((RuntimeError, ValueError), match="retry the request"):
        await run.resume((call,), approve=True, prompt="Multiply 6 by 7.")

    assert run.executed == []
    assert code.requests == []
    assert run.stored().status is RunStatus.paused
