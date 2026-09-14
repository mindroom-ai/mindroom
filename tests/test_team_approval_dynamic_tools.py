"""Team approval reconstruction retains the frozen function owner for each call."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.event_journal.approval_continuations import ApprovalDecision
from mindroom.response_turn import CompletedApprovalRun
from mindroom.synthetic_model import SyntheticModel
from mindroom.teams import TeamMode, continue_paused_team_run
from mindroom.tool_system import dynamic_toolkits
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.conftest import bind_runtime_paths, test_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from agno.agent import Agent


class _MemberAssemblyObservedError(Exception):
    """Stop after observing real rebuilt members, before unrelated team history reads."""


@pytest.mark.asyncio
async def test_team_approval_forwards_frozen_invoking_member_functions(tmp_path: Path) -> None:
    """The coordinator cannot replace frozen member ownership while rebuilding tools."""
    runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
    target = _target(thread_id="$thread")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="research",
        requester_id="@user:example.org",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id="team-session",
    )
    calls = tuple(
        ApprovalCall(
            tool_call_id=call_id,
            tool_name=function_name,
            invoking_agent=member_name,
            expires_at_ns=2**62,
            decision=ApprovalDecision.APPROVED,
            human_approval_required=True,
        )
        for call_id, function_name, member_name in (
            ("call-one", "add", "alpha"),
            ("call-two", "subtract", "alpha"),
            ("call-three", "sleep", "beta"),
        )
    )
    continuation = ApprovalContinuation(
        approval_id="team-approval",
        run_id="paused-team-run",
        session_id="team-session",
        entity_kind="team",
        entity_name="research",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        requester_id=identity.requester_id,
        response_event_id="$waiting",
        source_event_ids=("$source",),
        calls=calls,
        state="claimed",
        execution_identity=serialize_tool_execution_identity(identity),
        runtime_model_name="default",
        team_member_names=("alpha", "beta"),
        team_mode="coordinate",
    )
    continued = AsyncMock(return_value=CompletedApprovalRun(response_text="done", metadata_content={}))
    with (
        patch.object(
            runner.deps.tool_runtime,
            "build_dispatch_context",
            return_value=ToolDispatchContext(execution_identity=identity),
        ),
        patch("mindroom.response_runner.continue_paused_team_run", new=continued),
        patch("mindroom.response_runner.typing_indicator", _noop_typing),
    ):
        result = await runner._continue_entity_call(
            continuation,
            request=_plain_request(target, source_event_id="$source"),
            target=target,
            tool_trace_collector=[],
        )
    assert isinstance(result, CompletedApprovalRun)
    assert continued.await_args.kwargs.get("required_function_names") == {
        "alpha": frozenset({"add", "subtract"}),
        "beta": frozenset({"sleep"}),
    }
    assert continued.await_args.kwargs["member_names"] == ("alpha", "beta")
    assert continued.await_args.kwargs["decisions"] == dict.fromkeys((call.tool_call_id for call in calls), True)


@pytest.mark.asyncio
async def test_team_approval_restores_tools_only_for_the_frozen_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real team member assembly cannot give a sibling the paused owner's required tools."""
    await _exercise_team_member_assembly(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_team_approval_discovers_independent_member_tools_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One member's catalog request cannot prevent another member's discovery from starting."""
    barrier = asyncio.Barrier(2)
    beta_completed = asyncio.Event()

    async def resolve_member_tools(
        name: str,
        function_names: frozenset[str],
        **_kwargs: object,
    ) -> tuple[str, ...]:
        await barrier.wait()
        if name == "alpha":
            await beta_completed.wait()
            assert function_names == frozenset({"add"})
            return ("calculator",)
        assert name == "beta"
        assert function_names == frozenset()
        beta_completed.set()
        return ()

    async with asyncio.timeout(10):
        await _exercise_team_member_assembly(tmp_path, monkeypatch, required_resolver=resolve_member_tools)


@pytest.mark.asyncio
async def test_team_approval_failed_discovery_cancels_and_drains_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed approval reconstruction must settle other catalog requests before returning."""
    barrier = asyncio.Barrier(2)
    sibling_settled = asyncio.Event()

    async def resolve_member_tools(name: str, *_args: object, **_kwargs: object) -> tuple[str, ...]:
        if name == "alpha":
            await barrier.wait()
            msg = "Synthetic catalog unavailable"
            raise RuntimeError(msg)
        try:
            await barrier.wait()
            await asyncio.Event().wait()
        finally:
            sibling_settled.set()
        msg = "Sibling discovery unexpectedly completed"
        raise AssertionError(msg)

    async with asyncio.timeout(10):
        with pytest.raises(ExceptionGroup) as failure:
            await _exercise_team_member_assembly(tmp_path, monkeypatch, required_resolver=resolve_member_tools)
    assert len(failure.value.exceptions) == 1
    assert isinstance(failure.value.exceptions[0], RuntimeError)
    assert str(failure.value.exceptions[0]) == "Synthetic catalog unavailable"
    assert sibling_settled.is_set()


async def _exercise_team_member_assembly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    required_resolver: Callable[..., Awaitable[tuple[str, ...]]] | None = None,
) -> None:
    """Inspect real member construction after the optional catalog transport boundary."""
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": [], "learning": False},
                "agents": {
                    name: {"display_name": name.title(), "tools": [{"calculator": {"defer": True}}]}
                    for name in ("alpha", "beta")
                },
                "teams": {
                    "research": {
                        "display_name": "Research",
                        "role": "Coordinate synthetic calculations",
                        "agents": ["alpha", "beta"],
                    },
                },
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "tool_approval": {
                    "default": "auto_approve",
                    "rules": [{"match": "add", "action": "require_approval"}],
                },
            },
        ),
        paths,
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="research",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="team-session",
    )
    monkeypatch.setattr(
        "mindroom.agents._load_agent_model_instance",
        lambda *_args, **_kwargs: SyntheticModel(id="synthetic", tool_call_probability=0),
    )
    if required_resolver is not None:
        monkeypatch.setattr("mindroom.teams.required_approval_tool_names", required_resolver)

    def inspect_members(*, agents: list[Agent], **_kwargs: object) -> None:
        functions = {
            agent.id: {name for toolkit in agent.tools or [] for name in (*toolkit.functions, *toolkit.async_functions)}
            for agent in agents
        }
        assert set(functions) == {"alpha", "beta"}
        assert "add" in functions["alpha"]
        assert "add" not in functions["beta"]
        assert dynamic_toolkits._loaded_tools == {}
        raise _MemberAssemblyObservedError

    dynamic_toolkits._loaded_tools.clear()
    monkeypatch.setattr("mindroom.teams.open_bound_scope_session_context", inspect_members)
    try:
        with pytest.raises(_MemberAssemblyObservedError):
            await continue_paused_team_run(
                member_names=("alpha", "beta"),
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                session_id="team-session",
                run_id="paused-team-run",
                user_id=identity.requester_id,
                configured_team_name="research",
                model_name="default",
                decisions={"call-one": True},
                denial_reasons={"call-one": None},
                refresh_scheduler=None,
                required_function_names={"alpha": frozenset({"add"})},
            )
    finally:
        dynamic_toolkits._loaded_tools.clear()
