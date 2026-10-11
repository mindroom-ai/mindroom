"""Persisted CLI approval fields are decoded once before recovery."""

import pytest
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement


def test_cli_approval_payload_requires_and_retains_delegation_depth() -> None:
    """A recovery payload cannot silently reset a missing delegation depth."""
    from mindroom.agent_cli.approval import CliApprovalCall  # noqa: PLC0415

    requirement = RunRequirement(ToolExecution(tool_call_id="call", tool_name="load_tool"))
    payload = {
        "kind": "agent_cli",
        "toolkit": "dynamic_tools",
        "function": "load_tool",
        "arguments": {"tool_name": "sleep"},
        "call_id": "call",
        "parent_bash_call_id": "bash",
        "requirements": [requirement.to_dict()],
    }
    with pytest.raises(ValueError, match="delegation_depth"):
        CliApprovalCall.from_dict(payload)
    payload["delegation_depth"] = 2
    decoded = CliApprovalCall.from_dict(payload)
    assert decoded.delegation_depth == 2
    assert decoded.requirements[0].tool_execution.tool_call_id == "call"
    assert decoded.to_dict() == {**payload, "external_requirement": None}
    payload["arguments"]["tool_name"] = "changed"
    assert decoded.arguments == {"tool_name": "sleep"}


def test_cli_approval_payload_rejects_inconsistent_saved_identity() -> None:
    """A mismatched delegation requirement never decodes as its saved call."""
    from mindroom.agent_cli.approval import CliApprovalCall  # noqa: PLC0415

    external = RunRequirement(ToolExecution(tool_call_id="call", tool_name="run_subagent", tool_args={"task": "x"}))
    payload = {
        "kind": "agent_cli",
        "toolkit": "delegate",
        "function": "run_subagent",
        "arguments": {"task": "x"},
        "call_id": "call",
        "parent_bash_call_id": "bash",
        "requirements": [],
        "delegation_depth": 0,
        "external_requirement": external.to_dict(),
    }
    assert CliApprovalCall.from_dict(payload).external_requirement is not None
    for changed in ({"call_id": "other"}, {"function": "continue_subagent"}, {"arguments": {"task": "y"}}):
        with pytest.raises(ValueError, match="exact saved call"):
            CliApprovalCall.from_dict({**payload, **changed})
