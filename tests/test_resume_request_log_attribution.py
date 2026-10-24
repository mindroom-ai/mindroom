"""A resumed run's model requests are attributed to the turn they continue, as that turn's fresh requests are.

Each end-to-end test runs a real reply that pauses for approval and resumes it through the production approval
execution, with every scripted model behind the request log. The resumed requests' JSONL request and response records
and ``LLM usage`` lines must name the same agent or team, session, room, thread, requester, and correlation id as the
fresh turn's, not the model key.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom.approval.resume import with_resumed_turn_log_context
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, DefaultsConfig
from mindroom.delegation.state import DelegationState
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.request_context import REQUEST_ATTRIBUTION_FIELDS, RequestAttribution
from mindroom.providers.pipeline.request_log import RequestLog, logged_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
    approval_calls_for,
    approval_executor,
    registered_report_tool,
    respond,
    respond_team,
    resume,
    resume_team,
    stored_run,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.asyncio

REQUESTER = "@alice:example.org"
ROOM = "!room:example.org"
THREAD = "$thread:example.org"
CORRELATION = "$request:example.org"


def _config(paths: RuntimePaths) -> Config:
    """Return ``code``, alone and as the only member of ``squad``, and ``leader``, which delegates to it.

    ``write_report`` needs approval.
    """
    config = with_responder_access(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", tools=["report"]),
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            },
            teams={
                "squad": TeamConfig(display_name="Squad", role="Writes reports.", agents=["code"], mode="coordinate"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"rules": [{"match": "write_report", "action": "require_approval"}]},
        ),
        "code",
        users=[REQUESTER],
    )
    entity_ids(config, paths)
    return config


def _identity(agent_name: str, session_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", agent_name, REQUESTER, ROOM, THREAD, THREAD, session_id)


def _logged(model: ScriptedModel, log_dir: Path) -> WrappedProvider:
    """Put ``model`` behind the request log under a model key no entity shares."""
    debug = DebugConfig(log_llm_requests=True, llm_request_log_dir=str(log_dir))
    log = RequestLog(model_name="scripted", configured_provider="openai", debug=debug, storage_root=log_dir)
    return WrappedProvider(model, partial(logged_stream, log=log))


def _attribution(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {field: entry.get(field) for field in REQUEST_ATTRIBUTION_FIELDS}


def _logged_attributions(log_dir: Path, logs: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], ...]:
    """Return the attribution of each request record, each response record, and each ``LLM usage`` line, in order."""
    [log_file] = log_dir.glob("llm-requests-*.jsonl")
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    requests = [_attribution(entry) for entry in entries if "messages" in entry]
    responses = [_attribution(entry) for entry in entries if entry.get("record") == "response"]
    usage_lines = [_attribution(entry) for entry in logs if entry["event"] == "LLM usage"]
    return requests, responses, usage_lines


def _turn(agent_id: str, session_id: str, *, reply_to_event_id: str | None = None) -> dict[str, Any]:
    return asdict(
        RequestAttribution(
            agent_id=agent_id,
            session_id=session_id,
            room_id=ROOM,
            thread_id=THREAD,
            reply_to_event_id=reply_to_event_id,
            requester_id=REQUESTER,
            correlation_id=CORRELATION,
        ),
    )


@pytest.mark.parametrize("team", [False, True])
async def test_an_approval_resume_attributes_its_requests_to_the_paused_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    team: bool,
) -> None:
    """An agent's or a team's resumed requests log the agent or team, session, and turn the paused reply had."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    entity = "squad" if team else "code"
    identity = _identity(entity, "session")
    log_dir = tmp_path / "llm-requests"
    code = ScriptedModel([calls(tool_call("write_report", "call-1")), answer("Report written.")])
    leader = ScriptedModel(
        [calls(tool_call("delegate_task_to_member", "member", member_id="code", task="Write it")), answer("Done.")],
    )
    ScriptedAgents({"code": _logged(code, log_dir)}, team=_logged(leader, log_dir) if team else None).install(
        monkeypatch,
    )
    options = {"config": config, "runtime_paths": paths, "identity": identity, "correlation_id": CORRELATION}

    with registered_report_tool(), capture_logs() as logs:
        paused = (
            await respond_team("squad", "Write the report", **options)
            if team
            else await respond("code", "Write the report", **options)
        )
        assert isinstance(paused, PausedAttempt)
        approvals = approval_calls_for(paused, default_agent="code", toolkit_names={"write_report": "report"})
        decisions = {call.tool_call_id: True for call in approvals}
        result = (
            await resume_team("squad", paused, approvals, **options, decisions=decisions)
            if team
            else await resume(
                approval_executor(config, paths),
                paused,
                approvals,
                agent_name="code",
                **options,
                decisions=decisions,
            )
        )

    assert isinstance(result, CompletedApprovalRun)
    # The fresh turn's requests, then the resumed ones: a team's leader and member requests all carry the team.
    requests = 4 if team else 2
    expected = [_turn(entity, "session")] * requests
    assert _logged_attributions(log_dir, logs) == (expected, expected, expected)


async def test_a_resumed_delegated_child_attributes_its_requests_to_the_child_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child resumed by its parent's approval logs its own agent and session, as its fresh requests did."""
    paths = _runtime_paths(tmp_path)
    config = _config(paths)
    identity = _identity("leader", "parent")
    log_dir = tmp_path / "llm-requests"
    leader = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate-0", agent_name="code", task="Write the report")),
            answer("Parent done."),
        ],
    )
    code = ScriptedModel([calls(tool_call("write_report", "call-1")), answer("Child done.")])
    ScriptedAgents({"leader": _logged(leader, log_dir), "code": _logged(code, log_dir)}).install(monkeypatch)
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    with registered_report_tool(), capture_logs() as logs:
        paused = await respond("leader", "Delegate", **options, run_id="parent-run", correlation_id=CORRELATION)
        assert isinstance(paused, PausedAttempt)
        state = DelegationState.from_metadata(stored_run("leader", "parent-run", **options).metadata)
        [child] = state.children
        approvals = _saved_approval_calls(state)
        result = await resume(
            approval_executor(config, paths),
            paused,
            approvals,
            agent_name="leader",
            **options,
            decisions={call.tool_call_id: True for call in approvals},
            correlation_id=CORRELATION,
        )

    assert isinstance(result, CompletedApprovalRun)
    parent_turn = _turn("leader", "parent")
    child_turn = _turn("code", child.session_id)
    # The parent's request, the child's until its pause, the child's resumed request, and the parent's continuation.
    expected = [parent_turn, child_turn, child_turn, parent_turn]
    assert _logged_attributions(log_dir, logs) == (expected, expected, expected)


async def test_a_resume_attributes_its_requests_to_the_event_its_turn_replies_to(tmp_path: Path) -> None:
    """A resume of a reply to a source event logs that event as the reply target of its requests."""
    paths = _runtime_paths(tmp_path)
    log_dir = tmp_path / "llm-requests"
    model = _logged(ScriptedModel([answer("Report written.")]), log_dir)
    context = _delegate_runtime_context(
        _config(paths),
        paths,
        execution_identity=_identity("code", "session"),
        agent_name="code",
        correlation_id=CORRELATION,
    )
    source = "$source:example.org"
    context = replace(context, target=replace(context.target, reply_to_event_id=source))
    request = ModelRequest(messages=[Message(role="user", content="Go")])

    with tool_runtime_context(context), capture_logs() as logs:
        events = with_resumed_turn_log_context(model.stream(request))
        assert [event async for event in events]

    expected = [_turn("code", "session", reply_to_event_id=source)]
    assert _logged_attributions(log_dir, logs) == (expected, expected, expected)
