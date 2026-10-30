"""Load a paused ``code`` run from its pre-resume dump and continue it, as the capture's approval resumes did.

Each resume writes the sessions one approval resume read, as the capture dumped them just before the resume, where the
``code`` and ``helper`` agents' stores live, opens those stores, which converts the dumps as an upgrade does, and
continues the paused run through ``AgentApprovalExecution`` with the decisions the capture took. ``AGENT_RESUMES`` holds the resumes whose run metadata the ``io.mindroom.ai_run``
golden pins, with the answers the capture's models gave.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.config.main import Config
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.delegation import records
from mindroom.delegation.audit import start_child_record
from mindroom.delegation.state import DelegationState, child_execution_identity
from mindroom.event_journal import ApprovalDecision
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.response_turn import paused_attempt_from_response
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.session_ids import create_session_id
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.schema import TableNames
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.delegation_helpers import ScriptedAgents, approval_calls_for, approval_executor, resume
from tests.fakes.scripted_model import ScriptStep, answer, usage
from tests.session_storage.goldens import STORAGE
from tests.teams.squad import scripted

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.response_turn import PausedAttempt
    from mindroom.session_storage.store import ConversationStore
    from tests.fakes.scripted_model import ScriptedModel

ROOM = "!room:example.org"
_REQUESTER = "@alice:example.org"
# The capture's agents and models, as far as the ``code`` agent's resumes reach them.
_CONFIG: dict[str, Any] = {
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
            "access": {"users": [_REQUESTER]},
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
class CodeResume:
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
        """Return the run as the store holds it now."""
        record = self.store.get_run(self.paused.run_id)
        assert isinstance(record, RunRecord)
        return record

    def calls(self) -> tuple[ApprovalCall, ...]:
        """Return the approval cards' calls for the pause as the reply published them."""
        return approval_calls_for(self.attempt, default_agent="code", toolkit_names={"multiply": "calculator"})

    async def resume(self, calls: tuple[ApprovalCall, ...], *, approve: bool, prompt: str) -> object:
        """Approve or deny ``calls`` as the requester and continue the run; a denial carries the capture's reason."""
        decision = ApprovalDecision.APPROVED if approve else ApprovalDecision.DENIED
        reason = None if approve else "Alice declined the multiplication."
        decided = tuple(replace(call, decision=decision, reason=reason) for call in calls)
        with approval_receipt_context(decided, run_id=self.paused.run_id):
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


def _open_dump(
    agent_name: str,
    config: Config,
    paths: RuntimePaths,
    identity: ToolExecutionIdentity,
    dump: str,
) -> ConversationStore:
    """Write one pre-resume dump where ``agent_name``'s store lives, then open the store, converting the dump."""
    root = resolve_agent_storage(agent_name, config, paths, execution_identity=identity).session_state_root
    path = TableNames(agent_name).database_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((STORAGE / f"{dump}.sql").read_text(encoding="utf-8"))
    return create_session_storage(agent_name, config, paths, identity)


async def setup_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread: str,
    run_id: str,
    code: ScriptedModel,
    helper: ScriptedModel | None = None,
) -> CodeResume:
    """Load the pre-resume dump of ``run_id`` from ``thread``, with ``code`` and ``helper`` answering those agents.

    A delegation's child gets its own store and audit record, as the capture's had.
    """
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate(_CONFIG), paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=_REQUESTER,
        room_id=ROOM,
        thread_id=thread,
        resolved_thread_id=thread,
        session_id=create_session_id(ROOM, thread),
        transport_agent_name="code",
    )
    store = _open_dump("code", config, paths, identity, f"agent_code__before_{run_id}")
    paused = store.get_run(run_id)
    assert isinstance(paused, RunRecord)
    assert paused.status is RunStatus.paused
    for child in DelegationState.from_metadata(paused.metadata).children:
        _open_dump("helper", config, paths, child_execution_identity(child), f"agent_helper__before_{run_id}")
        # The child's audit record is on disk beside its store; the capture started it on its frozen clock.
        monkeypatch.setattr(records, "_utc_timestamp", lambda: "2027-01-15T08:26:00+00:00")
        await start_child_record(
            child,
            parent_run_id=run_id,
            config=config,
            runtime_paths=paths,
            caller_execution_identity=identity,
            parent_delegation_id=None,
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
        toolkit_owners={("code", "multiply"): "calculator"},
    )
    assert attempt is not None
    return CodeResume(config, paths, identity, store, paused, attempt, code, helper, executed)


@dataclass(frozen=True)
class AgentResume:
    """One captured approval resume: the paused run, its decision and prompt, and the answers its models gave."""

    thread: str
    run_id: str
    approve: bool
    prompt: str
    code: list[ScriptStep]
    helper: list[ScriptStep] | None = None


AGENT_RESUMES = {
    resume.run_id: resume
    for resume in (
        AgentResume(
            "$approved",
            "run-approved",
            approve=True,
            prompt="Multiply 6 by 7.",
            code=answer("6 x 7 = 42.", usage(260, 9, time_to_first_token=0.25)),
        ),
        AgentResume(
            "$rejected",
            "run-rejected",
            approve=False,
            prompt="Multiply 6 by 7.",
            code=answer(
                "I did not multiply, because the call was declined.",
                usage(260, 9, time_to_first_token=0.25),
            ),
        ),
        AgentResume(
            "$delegation-approved",
            "run-delegation-approved",
            approve=True,
            prompt="Ask the helper to multiply 6 by 7.",
            code=answer("The helper found 42.", usage(330, 6, time_to_first_token=0.25)),
            helper=answer("42", usage(100, 1, time_to_first_token=0.25)),
        ),
    )
}


async def setup_case(case: AgentResume, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodeResume:
    """Load the captured pause of ``case``, with its models scripted to answer as the capture's did."""
    code = scripted("code-model", case.code)
    helper = None if case.helper is None else scripted("helper-model", case.helper)
    return await setup_resume(tmp_path, monkeypatch, thread=case.thread, run_id=case.run_id, code=code, helper=helper)


async def resume_case(case: AgentResume, run: CodeResume) -> object:
    """Continue the loaded pause of ``case`` with its decision and prompt, and return what the resume returned."""
    return await run.resume(run.calls(), approve=case.approve, prompt=case.prompt)
