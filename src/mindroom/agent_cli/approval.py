"""Typed CLI approval state at the builders and recovery boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import cast

from agno.run.requirement import RunRequirement


@dataclass(frozen=True)
class CliApprovalCall:
    """One exact hidden call; the journal retains its existing JSON wire shape."""

    toolkit: str
    function: str
    arguments: dict[str, object]
    call_id: str
    parent_bash_call_id: str
    requirements: tuple[RunRequirement, ...]
    delegation_depth: int
    external_requirement: RunRequirement | None = None

    def to_dict(self) -> dict[str, object]:
        """Take an independent wire snapshot before handing ownership to the journal."""
        return {
            "kind": "agent_cli",
            "toolkit": self.toolkit,
            "function": self.function,
            "arguments": deepcopy(self.arguments),
            "call_id": self.call_id,
            "parent_bash_call_id": self.parent_bash_call_id,
            "requirements": [item.to_dict() for item in self.requirements],
            "delegation_depth": self.delegation_depth,
            "external_requirement": self.external_requirement.to_dict() if self.external_requirement else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CliApprovalCall:
        """Reject incomplete or inconsistent persisted identity instead of resetting delegation limits."""
        if payload.get("kind") != "agent_cli":
            msg = "CLI approval requires its agent_cli payload"
            raise ValueError(msg)
        names = ("toolkit", "function", "call_id", "parent_bash_call_id")
        for name in names:
            if not isinstance(payload.get(name), str) or not payload[name]:
                msg = f"CLI approval requires {name}"
                raise ValueError(msg)
        depth = payload.get("delegation_depth")
        if type(depth) is not int or depth < 0:
            msg = "CLI approval requires a nonnegative delegation_depth"
            raise ValueError(msg)
        arguments = payload.get("arguments")
        requirements = payload.get("requirements")
        external = payload.get("external_requirement")
        if (
            not isinstance(arguments, dict)
            or not isinstance(requirements, list)
            or any(not isinstance(item, dict) for item in requirements)
            or (external is not None and not isinstance(external, dict))
        ):
            msg = "CLI approval requires its exact arguments and requirements"
            raise ValueError(msg)
        external_requirement = (
            RunRequirement.from_dict(deepcopy(cast("dict[str, object]", external))) if external is not None else None
        )
        if external_requirement is not None and (
            (execution := external_requirement.tool_execution) is None
            or execution.tool_call_id != payload["call_id"]
            or execution.tool_name != payload["function"]
            or execution.tool_args != arguments
        ):
            msg = "CLI delegation requires its exact saved call"
            raise ValueError(msg)
        return cls(
            toolkit=cast("str", payload["toolkit"]),
            function=cast("str", payload["function"]),
            arguments=deepcopy(cast("dict[str, object]", arguments)),
            call_id=cast("str", payload["call_id"]),
            parent_bash_call_id=cast("str", payload["parent_bash_call_id"]),
            requirements=tuple(
                RunRequirement.from_dict(deepcopy(item)) for item in cast("list[dict[str, object]]", requirements)
            ),
            delegation_depth=depth,
            external_requirement=external_requirement,
        )
