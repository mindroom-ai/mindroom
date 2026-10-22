"""Team approval reconstruction retains the frozen function owner for each call."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.event_journal import ApprovalCall
from mindroom.event_journal.approval_continuations import ApprovalDecision
from mindroom.history.session_context import open_resolved_scope_session_context
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.response_turn import CompletedApprovalRun
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.approval_continuation_helpers import approval_continuation, resumed_approval
from tests.conftest import bind_runtime_paths, test_runtime_paths, unwrap_extracted_collaborator
from tests.fakes.scripted_model import ScriptedModel
from tests.reply_span_helpers import paused_for_approval
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.teams.members import ResolvedExactTeamMembers


class _MemberAssemblyObservedError(Exception):
    """Stop after observing the rebuilt members, before the team is built and the run resumes."""


@pytest.mark.asyncio
async def test_team_approval_forwards_frozen_invoking_member_functions(tmp_path: Path) -> None:
    """The coordinator cannot replace frozen member ownership while rebuilding tools."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    await _admit_approval_source(runner.deps.approval_store)
    target = _target(thread_id="$thread")
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
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
            toolkit_name="sleep" if member_name == "beta" else "calculator",
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
    continuation = approval_continuation(
        approval_id="team-approval",
        run_id="paused-team-run",
        session_id="team-session",
        entity_kind="team",
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        requester_id=identity.requester_id,
        calls=calls,
        execution_identity=serialize_tool_execution_identity(identity),
        runtime_model_name="default",
        team_member_names=("alpha", "beta"),
        team_mode="coordinate",
    )
    assert await paused_for_approval(runner.deps.approval_store, continuation) is not None
    continued = AsyncMock(return_value=CompletedApprovalRun(response_text="done", metadata_content={}))
    progress = AsyncMock()
    with (
        patch.object(
            runner.deps.tool_runtime,
            "build_dispatch_context",
            return_value=ToolDispatchContext(execution_identity=identity),
        ),
        patch("mindroom.response_runner.continue_paused_team_run", new=continued),
        patch("mindroom.response_runner.typing_indicator", _noop_typing),
    ):
        async with resumed_approval(bot, continuation) as claimed:
            result = await runner._continue_entity_call(
                claimed,
                request=_plain_request(target, source_event_id="$source"),
                target=target,
                tool_trace_collector=[],
                progress=progress,
            )
    assert isinstance(result, CompletedApprovalRun)
    assert continued.await_args.kwargs["progress"] is progress
    assert continued.await_args.kwargs["approval_calls"] == calls
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
        calls: tuple[ApprovalCall, ...],
        **_kwargs: object,
    ) -> tuple[str, ...]:
        await barrier.wait()
        if name == "alpha":
            await beta_completed.wait()
            assert [(call.tool_name, call.toolkit_name) for call in calls] == [("add", "calculator")]
            return ("calculator",)
        assert name == "beta"
        assert calls == ()
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
    """Inspect the rebuilt members after the optional catalog transport boundary, before the team runs."""
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate(
            {
                "defaults": {"tools": []},
                # Each member owns a different deferred toolkit, so a sibling never gains the paused owner's tools.
                "agents": {
                    "alpha": {"display_name": "Alpha", "tools": [{"calculator": {"defer": True}}]},
                    "beta": {"display_name": "Beta", "tools": [{"sleep": {"defer": True}}]},
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
    monkeypatch.setattr("mindroom.agents._build_agent_model_provider", lambda *_args, **_kwargs: ScriptedModel([]))
    if required_resolver is not None:
        monkeypatch.setattr("mindroom.teams.resume.required_approval_tool_names", required_resolver)

    def inspect_members(*, members: ResolvedExactTeamMembers, **_kwargs: object) -> None:
        functions = {build.spec.id: set(build.tool_owners) for build in members.builds}
        assert set(functions) == {"alpha", "beta"}
        assert "add" in functions["alpha"]
        assert "add" not in functions["beta"]
        raise _MemberAssemblyObservedError

    with open_resolved_scope_session_context(
        agent_name="alpha",
        scope=HistoryScope(kind="team", scope_id="research"),
        session_id=identity.session_id,
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        create_session_if_missing=True,
    ) as scope:
        assert scope is not None
        assert scope.session is not None
        scope.storage.save_run(
            scope.session,
            TeamRunRecord(
                run_id="paused-team-run",
                session_id=identity.session_id,
                user_id=identity.requester_id,
                team_id="research",
                status=RunStatus.paused,
            ),
            record_usage=False,
        )
    monkeypatch.setattr("mindroom.teams.resume.build_team_spec", inspect_members)
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
            approval_calls=(
                ApprovalCall(
                    tool_call_id="call-one",
                    tool_name="add",
                    invoking_agent="alpha",
                    toolkit_name="calculator",
                    expires_at_ns=2**62,
                ),
            ),
            progress=None,
        )
