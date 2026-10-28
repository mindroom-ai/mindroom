"""Production approval resumes continue runs Agno stored paused into completed runs, and refuse what they must.

Each test loads a paused run from its pre-resume dump (``tests.approval_resume_helpers``), so a run paused before the
upgrade resumes after it, and continues it through ``AgentApprovalExecution`` with the decisions the capture took.
The stored records are asserted as typed values; ``tests/test_ai_run_metadata_goldens.py`` compares the run metadata
each resume publishes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState, child_execution_identity
from mindroom.event_journal import approval_arguments_digest
from mindroom.llm.provider import TurnCompleted
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, ToolExecution
from mindroom.response_turn import CompletedApprovalRun
from mindroom.session_storage.agent_storage import create_session_storage
from tests.approval_continuation_helpers import receipt_call_ids
from tests.approval_resume_helpers import AGENT_RESUMES, CodeResume, resume_case, setup_case, setup_resume
from tests.run_record_helpers import reply, result, run_metrics, settled
from tests.teams.squad import scripted

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.llm.messages import Message
    from tests.approval_resume_helpers import AgentResume

pytestmark = pytest.mark.asyncio

_PRODUCT = '{"operation": "multiplication", "result": 42.0}'


def _resumed(
    paused: RunRecord,
    case: AgentResume,
    *,
    decided: ToolExecution,
    added: Sequence[Message],
    **requirement_fields: str | bool,
) -> RunRecord:
    """Return ``paused`` as the store holds it after its resume answered as ``case``'s model did.

    Its one waiting call ends as ``decided``, its requirement records that call and ``requirement_fields``, and
    ``added`` and the final reply follow its messages; a finished run is stored without its system message.
    """
    [requirement] = paused.requirements or ()
    [step] = [step for step in case.code if isinstance(step, TurnCompleted)]
    text, metrics = step.message.content, step.message.metrics
    assert isinstance(text, str)
    return replace(
        paused,
        status=RunStatus.completed,
        content=text,
        messages=[
            *(message for message in paused.messages if message.role != "system"),
            *added,
            reply(text, model="code-model", metrics=metrics),
        ],
        tools=[decided],
        requirements=[replace(requirement, tool_execution=decided, **requirement_fields)],
        metrics=run_metrics(("code-model", metrics), start=paused.metrics),
    )


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

    resumed = await resume_case(case, run)

    assert isinstance(resumed, CompletedApprovalRun)
    assert run.executed == ([(6, 7)] if case.approve else [])
    [call] = run.paused.messages[-1].tool_calls or ()
    [waiting] = run.paused.tools or ()
    if case.approve:
        decided = replace(waiting, is_error=False, result=_PRODUCT, requires_confirmation=False, confirmed=True)
        expected = _resumed(run.paused, case, decided=decided, added=[result(call, _PRODUCT)], confirmation=True)
    else:
        reason = "Alice declined the multiplication."
        decided = replace(
            waiting,
            is_error=True,
            requires_confirmation=False,
            confirmed=False,
            confirmation_note=reason,
        )
        expected = _resumed(
            run.paused,
            case,
            decided=decided,
            added=[result(call, reason, is_error=True)],
            confirmation=False,
            confirmation_note=reason,
        )
    stored = run.stored()
    assert settled(replace(stored, metadata=None)) == settled(replace(expected, metadata=None))
    assert _without_published_metadata(stored) == _without_published_metadata(run.paused)
    assert run.paused.to_dict() == before
    # The resumed request carries the stored prompt, the turn, the decided call's result, then the receipt once.
    [request] = run.code.requests
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

    resumed = await resume_case(case, run)

    assert isinstance(resumed, CompletedApprovalRun)
    assert run.executed == [(6, 7)]
    # The child runs its approved call and answers, as a resumed run does.
    [child_call] = paused_child.messages[-1].tool_calls or ()
    [child_waiting] = paused_child.tools or ()
    assert case.helper is not None
    [child_answer] = [step for step in case.helper if isinstance(step, TurnCompleted)]
    approved = replace(child_waiting, is_error=False, result=_PRODUCT, requires_confirmation=False, confirmed=True)
    [child_requirement] = paused_child.requirements or ()
    child_record = child_store.get_run(child.run_id)
    assert isinstance(child_record, RunRecord)
    assert settled(child_record) == settled(
        replace(
            paused_child,
            status=RunStatus.completed,
            content="42",
            messages=[
                *(message for message in paused_child.messages if message.role != "system"),
                result(child_call, _PRODUCT),
                reply("42", model="helper-model", metrics=child_answer.message.metrics),
            ],
            tools=[approved],
            requirements=[replace(child_requirement, tool_execution=approved, confirmation=True)],
            metrics=run_metrics(("helper-model", child_answer.message.metrics), start=paused_child.metrics),
        ),
    )
    # The parent's delegation is answered with the child's answer and its record, as an externally executed result.
    handed_over = (
        f"42\n\nDelegation {child.delegation_id} record: helper:.mindroom/delegations/2027-01-15/{child.delegation_id}"
        f"\nSubagent ID: {child.subagent_id}"
    )
    [call] = run.paused.messages[-1].tool_calls or ()
    [waiting] = run.paused.tools or ()
    executed = replace(waiting, result=handed_over, external_execution_required=False)
    expected = _resumed(
        run.paused,
        case,
        decided=executed,
        added=[replace(result(call, handed_over), tool_call_error=None)],
        external_execution_result=handed_over,
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
        pending_requirements=[],
        pending_child_id=None,
        pending_agent_name=None,
    )
    assert _without_published_metadata(stored, DELEGATION_STATE_KEY) == _without_published_metadata(
        run.paused,
        DELEGATION_STATE_KEY,
    )
    # The parent reads the child's answer where it read the pause, on its own stored request, then the receipt.
    [request] = run.code.requests
    assert [message.role for message in request.messages] == ["system", "user", "assistant", "tool", "user"]
    assert request.messages[-2].tool_call_id == "call-delegate-approved"
    assert str(request.messages[-2].content).startswith("42")
    # The parent holds no such call, so it reads the card's id for the child's approved call.
    assert receipt_call_ids(request) == [call.tool_call_id for call in run.calls()]


def _save(run: CodeResume, record: RunRecord) -> None:
    session = run.store.load_session(record.session_id or "", "agent")
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
