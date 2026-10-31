"""Typed CLI approval state at the builders and recovery boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from mindroom.llm.runs import RunRequirement, ToolExecution


@dataclass(frozen=True)
class CliApprovalCall:
    """One exact hidden call; the journal retains its JSON wire shape.

    ``toolkit`` is the toolkit the call is approved under, which the resumed agent attaches and finds the binding of
    ``function`` by. ``requirements`` are the calls the approval asks about: the hidden call itself, or, for a
    delegation call, the calls its delegation asked about. A delegation call keeps its ``delegation_pause_id``, the
    id its child and hooks are kept under (``ToolExecution.pause_id``).
    """

    toolkit: str
    function: str
    arguments: dict[str, object]
    call_id: str
    parent_bash_call_id: str
    requirements: tuple[RunRequirement, ...]
    delegation_depth: int
    delegation_pause_id: str | None = None

    @property
    def external_requirement(self) -> RunRequirement | None:
        """The delegation call this approval continues, waiting for its answer; None for any other hidden call."""
        if self.delegation_pause_id is None:
            return None
        return RunRequirement(
            ToolExecution(
                tool_call_id=self.call_id,
                tool_name=self.function,
                tool_args=deepcopy(self.arguments),
                external_execution_required=True,
                pause_id=self.delegation_pause_id,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        """Take an independent wire snapshot before handing ownership to the journal."""
        return {
            "kind": "agent_cli",
            "toolkit": self.toolkit,
            "function": self.function,
            "arguments": deepcopy(self.arguments),
            "call_id": self.call_id,
            "parent_bash_call_id": self.parent_bash_call_id,
            "pending_calls": [item.tool_execution.to_dict() for item in self.requirements],
            "delegation_depth": self.delegation_depth,
            "delegation_pause_id": self.delegation_pause_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CliApprovalCall:
        """Reject incomplete persisted identity instead of resetting delegation limits."""
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
        pending, pause_id = _pending_calls(payload)
        if (
            not isinstance(arguments, dict)
            or not isinstance(pending, list)
            or any(not isinstance(item, dict) for item in pending)
            or (pause_id is not None and not isinstance(pause_id, str))
        ):
            msg = "CLI approval requires its exact arguments and pending calls"
            raise ValueError(msg)
        return cls(
            toolkit=cast("str", payload["toolkit"]),
            function=cast("str", payload["function"]),
            arguments=deepcopy(cast("dict[str, object]", arguments)),
            call_id=cast("str", payload["call_id"]),
            parent_bash_call_id=cast("str", payload["parent_bash_call_id"]),
            requirements=tuple(
                RunRequirement(ToolExecution.from_dict(item)) for item in cast("list[dict[str, object]]", pending)
            ),
            delegation_depth=depth,
            delegation_pause_id=pause_id,
        )


def _pending_calls(payload: dict[str, object]) -> tuple[object, object]:
    """Return a payload's pending calls and its delegation call's pause id, as stored or as an older payload holds them."""
    if "pending_calls" in payload:
        return payload["pending_calls"], payload.get("delegation_pause_id")
    # LEGACY_COMPAT: CLI approval payloads that store each pending call as a requirement.
    # Legacy format: an `agent_cli` payload in an approval continuation's `cli_call` whose `requirements` list holds
    #   one requirement dict per pending call, each with the call as `tool_execution`, and whose
    #   `external_requirement` holds the delegation call's requirement dict, `id` and `tool_execution`, or null.
    # Last legacy release: v2026.10.744, which wrote them as every earlier release did; replacement: the next release,
    #   whose payload holds `pending_calls` and `delegation_pause_id`.
    # Handling: the pending calls are the requirements' `tool_execution`s, and the delegation call keeps the stored
    #   requirement's `id` as its pause id; the call itself is read from the payload's own call fields, which the
    #   writer took from the same call. A payload whose entries are not dicts is refused as before.
    # Coverage: tests/test_cli_approval_payload.py::test_an_older_payload_reads_its_pending_calls_and_delegation_id
    requirements = payload.get("requirements")
    pending = (
        [cast("dict[str, Any]", item).get("tool_execution") for item in requirements]
        if isinstance(requirements, list) and all(isinstance(item, dict) for item in requirements)
        else requirements
    )
    external = payload.get("external_requirement")
    return pending, cast("dict[str, Any]", external).get("id") if isinstance(external, dict) else external
