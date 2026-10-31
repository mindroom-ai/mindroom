"""Persisted CLI approval fields are decoded once before recovery."""

import pytest

from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.llm.runs import RunRequirement, ToolExecution


def test_cli_approval_payload_requires_and_retains_delegation_depth() -> None:
    """A recovery payload cannot silently reset a missing delegation depth."""
    call = ToolExecution(tool_call_id="call", tool_name="load_tool", requires_confirmation=True)
    payload: dict[str, object] = {
        "kind": "agent_cli",
        "toolkit": "dynamic_tools",
        "function": "load_tool",
        "arguments": {"tool_name": "sleep"},
        "call_id": "call",
        "parent_bash_call_id": "bash",
        "pending_calls": [call.to_dict()],
        "delegation_pause_id": None,
    }
    with pytest.raises(ValueError, match="delegation_depth"):
        CliApprovalCall.from_dict(payload)
    payload["delegation_depth"] = 2
    decoded = CliApprovalCall.from_dict(payload)
    assert decoded.delegation_depth == 2
    assert decoded.requirements == (RunRequirement(call),)
    assert decoded.external_requirement is None
    assert decoded.to_dict() == payload
    payload["arguments"]["tool_name"] = "changed"  # type: ignore[index]
    assert decoded.arguments == {"tool_name": "sleep"}


def test_a_delegation_payload_keeps_its_call_once_and_its_pause_id() -> None:
    """The delegation call is the payload's own call, waiting for its answer under the id its child is kept by."""
    payload: dict[str, object] = {
        "kind": "agent_cli",
        "toolkit": "delegate",
        "function": "run_subagent",
        "arguments": {"task": "x"},
        "call_id": "call",
        "parent_bash_call_id": "bash",
        "pending_calls": [],
        "delegation_depth": 0,
        "delegation_pause_id": "pause-1",
    }

    decoded = CliApprovalCall.from_dict(payload)

    assert decoded.external_requirement == RunRequirement(
        ToolExecution(
            tool_call_id="call",
            tool_name="run_subagent",
            tool_args={"task": "x"},
            external_execution_required=True,
            pause_id="pause-1",
        ),
    )
    assert decoded.to_dict() == payload
    with pytest.raises(ValueError, match="pending calls"):
        CliApprovalCall.from_dict({**payload, "delegation_pause_id": 1})


def test_an_older_payload_reads_its_pending_calls_and_delegation_id() -> None:
    """A payload a release up to v2026.10.744 saved held each pending call as a requirement, and the delegation's too."""
    pending = {"tool_call_id": "child:c1", "tool_name": "add", "requires_confirmation": True}
    delegation = {"tool_call_id": "call", "tool_name": "run_subagent", "tool_args": {"task": "x"}}
    older: dict[str, object] = {
        "kind": "agent_cli",
        "toolkit": "delegate",
        "function": "run_subagent",
        "arguments": {"task": "x"},
        "call_id": "call",
        "parent_bash_call_id": "bash",
        "requirements": [{"id": "req-1", "created_at": "2026-10-01T00:00:00+00:00", "tool_execution": pending}],
        "delegation_depth": 0,
        "external_requirement": {"id": "parent-requirement", "tool_execution": delegation},
    }

    decoded = CliApprovalCall.from_dict(older)

    assert decoded.requirements == (RunRequirement(ToolExecution.from_dict(pending)),)
    assert decoded.delegation_pause_id == "parent-requirement"
    assert decoded.external_requirement is not None
    assert decoded.external_requirement.tool_execution.tool_args == {"task": "x"}
    hidden_call = CliApprovalCall.from_dict({**older, "external_requirement": None})
    assert hidden_call.external_requirement is None
    with pytest.raises(ValueError, match="pending calls"):
        CliApprovalCall.from_dict({**older, "requirements": [1]})
