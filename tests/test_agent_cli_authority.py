"""Turn-owned CLI authority and untrusted protocol contracts."""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mindroom.agent_cli.lifetime import response_cli_lifetime
from mindroom.agent_cli.protocol import ToolCallOperation, ToolCallReceipt, parse_operation
from mindroom.agent_cli.response_owner import bind_response_owner
from mindroom.agent_cli.session import (
    CliAuthenticationError,
    TurnToolBridge,
    cli_turn_owner,
)
from mindroom.agent_loop.spec import RunContext
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import RuntimePaths
from mindroom.message_target import MessageTarget
from mindroom.response_turn import ResponseTurnContext
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog
from mindroom.tool_system.output_files import ToolOutputFilePolicy
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, runtime_paths_for
from tests.minimal_agent_fixtures import (  # noqa: F401 - agent_cli_api is a pytest fixture
    agent_cli_api,
    settled_receipt,
)
from tests.runtime_fakes import ready_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _runtime_context(tmp_path: Path) -> ToolRuntimeContext:
    paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
    )
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        paths,
    )
    return replace(
        make_test_tool_runtime_context(
            agent_name="helper",
            target=MessageTarget(
                room_id="!room:example.test",
                source_thread_id="$thread:example.test",
                resolved_thread_id="$thread:example.test",
                reply_to_event_id="$reply:example.test",
                session_id="session-1",
            ),
            requester_id="@alice:example.test",
            client=SimpleNamespace(),
            config=config,
            runtime_paths=runtime_paths_for(config),
            relations=make_relation_lookup(),
            conversation_reader=make_conversation_reader_mock(),
        ),
        correlation_id="correlation-1",
        membership_turn_id="$driving:example.test",
    )


def _turn_context() -> ResponseTurnContext:
    return ResponseTurnContext(
        entity_label="helper",
        session_id="session-1",
        run_id="generation-1",
        correlation_id="correlation-1",
        reply_to_event_id="$reply:example.test",
        room_id="!room:example.test",
        thread_id="$thread:example.test",
        requester_id="@alice:example.test",
        matrix_run_metadata=None,
    )


def test_owner_factory_binds_exact_runtime_and_turn_identity(tmp_path: Path) -> None:
    owner = cli_turn_owner(_runtime_context(tmp_path), _turn_context())

    assert owner.turn_id == "$driving:example.test"
    assert owner.generation == "generation-1"
    assert owner.execution_identity.agent_name == "helper"
    assert owner.execution_identity.requester_id == "@alice:example.test"


@pytest.mark.asyncio
@pytest.mark.usefixtures("agent_cli_api")
async def test_a_reload_that_changed_the_agents_tools_refuses_a_cli_call_before_binding_its_toolkit(
    tmp_path: Path,
) -> None:
    """A catalog a reload invalidated builds no toolkit for a CLI call; the call fails as unavailable."""
    runtime = _runtime_context(tmp_path)
    current = runtime.config.model_copy(deep=True)
    current.agents["helper"].tools = ["calculator"]
    runtime = replace(
        runtime,
        orchestrator=ready_runtime(runtime.config, runtime.runtime_paths),
        config_provider=lambda: current,
    )
    session = ConversationSession(session_id="session-1", kind="agent", owner_id="helper")
    catalog = PreparedAgentToolCatalog(
        run=RunContext(run_id="generation-1", user_id=runtime.requester_id, session=session),
        runtime_context=runtime,
        store=None,
        middleware=(),
        max_tool_calls_per_turn=None,
    )
    catalog.bind = AsyncMock()
    async with response_cli_lifetime() as lifetime:
        owner = bind_response_owner(
            catalog,
            lifetime=lifetime,
            response_context=_turn_context(),
            context_documents={},
            output_file_policy=ToolOutputFilePolicy(tmp_path / "workspace"),
            delegation_depth=0,
            refresh_scheduler=None,
            failure_message=str,
        )
        async with owner._window("bash"):
            receipt = await owner.operation(
                window="bash",
                operation=ToolCallOperation(
                    operation="tools.call",
                    call_id=uuid4(),
                    toolkit="calculator",
                    function="add",
                ),
            )
            receipt = await settled_receipt(owner, receipt["call_id"])

    assert (receipt["status"], receipt["outcome"]) == ("failed", "Tool is unavailable or arguments are invalid")
    catalog.bind.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("entity_label", "other"),
        ("session_id", "other"),
        ("run_id", None),
        ("correlation_id", "other"),
        ("reply_to_event_id", "$other"),
        ("room_id", "!other:example.test"),
        ("thread_id", "$other"),
        ("requester_id", "@mallory:example.test"),
    ],
)
def test_owner_factory_rejects_mismatched_or_missing_turn_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="CLI turn owner"):
        cli_turn_owner(_runtime_context(tmp_path), replace(_turn_context(), **{field: value}))


def test_owner_factory_rejects_team_context_and_missing_turn(tmp_path: Path) -> None:
    runtime = _runtime_context(tmp_path)

    with pytest.raises(ValueError, match="agent dispatch"):
        cli_turn_owner(replace(runtime, agent_name="team"), replace(_turn_context(), entity_label="team"))
    with pytest.raises(ValueError, match="turn"):
        cli_turn_owner(replace(runtime, membership_turn_id=None), _turn_context())


@pytest.mark.parametrize("revoke_first", [False, True])
def test_bridge_cannot_issue_another_grant(tmp_path: Path, revoke_first: bool) -> None:
    owner = cli_turn_owner(_runtime_context(tmp_path), _turn_context())
    bridge = TurnToolBridge(owner)
    bridge.issue(now_ns=10, expires_at_ns=20)
    if revoke_first:
        bridge.revoke()
    with pytest.raises(RuntimeError, match="grant"):
        bridge.issue(now_ns=30, expires_at_ns=100)


def test_bridge_hashes_tokens_caps_expiry_and_authenticates_bearer(tmp_path: Path) -> None:
    owner = cli_turn_owner(_runtime_context(tmp_path), _turn_context())
    bridge = TurnToolBridge(owner)

    grant = bridge.issue(now_ns=10, expires_at_ns=10 + 48 * 60 * 60 * 1_000_000_000)

    assert grant.raw_token
    assert grant.raw_token not in repr(grant)
    assert grant.expires_at_ns == 10 + 24 * 60 * 60 * 1_000_000_000
    assert grant.raw_token not in repr(bridge._grant)
    assert bridge.authenticate(grant.raw_token, now_ns=11) == owner
    with pytest.raises(CliAuthenticationError):
        bridge.authenticate("wrong-token", now_ns=11)


def test_bridge_expiry_revocation_resume_and_restart_fail_closed(tmp_path: Path) -> None:
    owner = cli_turn_owner(_runtime_context(tmp_path), _turn_context())
    bridge = TurnToolBridge(owner)
    expired = bridge.issue(now_ns=10, expires_at_ns=20)
    with pytest.raises(CliAuthenticationError):
        bridge.authenticate(expired.raw_token, now_ns=20)

    bridge = TurnToolBridge(owner)
    revoked = bridge.issue(now_ns=30, expires_at_ns=100)
    bridge.revoke()
    with pytest.raises(CliAuthenticationError):
        bridge.authenticate(revoked.raw_token, now_ns=31)

    resumed_owner = replace(owner, generation="generation-2")
    resumed_bridge = TurnToolBridge(resumed_owner)
    resumed = resumed_bridge.issue(now_ns=40, expires_at_ns=100)
    assert resumed_bridge.authenticate(resumed.raw_token, now_ns=41) == resumed_owner
    with pytest.raises(CliAuthenticationError):
        TurnToolBridge(resumed_owner).authenticate(resumed.raw_token, now_ns=41)


def test_call_payload_cannot_choose_identity_or_worker() -> None:
    base = {
        "operation": "tools.call",
        "call_id": "12345678-1234-4234-8234-123456789abc",
        "toolkit": "calculator",
        "function": "add",
        "arguments": {"a": 1, "nested": {"requester_id": "legitimate-tool-data"}},
    }
    for forbidden in ("requester_id", "agent_name", "worker_id", "turn_id", "generation", "room_id"):
        with pytest.raises(ValidationError):
            ToolCallOperation.model_validate({**base, forbidden: "forged"})
    assert ToolCallOperation.model_validate(base).arguments["nested"] == {"requester_id": "legitimate-tool-data"}


def test_protocol_requires_uuid_object_arguments_and_known_operation() -> None:
    base = {
        "operation": "tools.call",
        "call_id": "12345678-1234-4234-8234-123456789abc",
        "toolkit": "calculator",
        "function": "add",
        "arguments": {},
    }
    assert isinstance(parse_operation(base), ToolCallOperation)
    assert isinstance(parse_operation({**base, "call_id": "12345678-1234-1234-8234-123456789abc"}), ToolCallOperation)
    with pytest.raises(ValidationError):
        parse_operation({**base, "call_id": "not-a-uuid"})
    with pytest.raises(ValidationError):
        parse_operation({**base, "arguments": [1, 2]})
    with pytest.raises(ValidationError):
        parse_operation({"operation": "arbitrary.method"})


def test_call_canonical_arguments_reject_nonfinite_values_and_have_stable_serialization() -> None:
    operation = ToolCallOperation.model_validate(
        {
            "operation": "tools.call",
            "call_id": "12345678-1234-4234-8234-123456789abc",
            "toolkit": "calculator",
            "function": "add",
            "arguments": {"b": 2, "a": 1},
        },
    )
    assert operation.canonical_arguments_json == '{"a":1,"b":2}'
    with pytest.raises(ValidationError, match="strict JSON"):
        ToolCallOperation.model_validate(
            {
                "operation": "tools.call",
                "call_id": "12345678-1234-4234-8234-123456789abc",
                "toolkit": "calculator",
                "function": "add",
                "arguments": {"value": float("nan")},
            },
        )
    with pytest.raises(ValidationError, match="64 KiB"):
        ToolCallOperation.model_validate(
            {
                "operation": "tools.call",
                "call_id": "12345678-1234-4234-8234-123456789abc",
                "toolkit": "calculator",
                "function": "add",
                "arguments": {"value": "x" * (64 * 1024)},
            },
        )


def test_receipt_model_forbids_identity_and_routing_fields() -> None:
    receipt = {
        "call_id": "12345678-1234-4234-8234-123456789abc",
        "toolkit": "calculator",
        "function": "add",
        "status": "queued",
    }
    assert ToolCallReceipt.model_validate(receipt).status == "queued"
    with pytest.raises(ValidationError):
        ToolCallReceipt.model_validate({**receipt, "requester_id": "@mallory:example.test"})
