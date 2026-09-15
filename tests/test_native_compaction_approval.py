"""Approval reconstruction preserves the paused provider's native replay policy."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.team import Team
from agno.tools.function import Function
from openai import AsyncOpenAI

from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import TeamMode, _TeamStreamPresentation, continue_paused_team_run
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import runtime_paths_for, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing
from tests.test_openai_native_compaction import _ANSWER, _CALL, _CHECKPOINT, _REASONING, _event, _response
from tests.test_team_response import _build_test_config

if TYPE_CHECKING:
    from pathlib import Path


async def _resume_approval(
    actor: Agent | Team,
    *,
    identity: ToolExecutionIdentity,
    run_id: str,
    tool_call_id: str,
    tmp_path: Path,
) -> CompletedApprovalRun | PausedAttempt:
    """Enter the production agent or team approval boundary with a fresh actor."""
    calls = (
        ApprovalCall(
            tool_call_id=tool_call_id,
            tool_name="lookup",
            invoking_agent=identity.agent_name,
            expires_at_ns=2**62,
        ),
    )
    if isinstance(actor, Agent):
        continuation = ApprovalContinuation(
            approval_id="approval-native",
            run_id=run_id,
            session_id="session-1",
            entity_kind="agent",
            entity_name=identity.agent_name,
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            response_event_id="$waiting",
            calls=calls,
            execution_identity={},
            sources=ResponseSources(("$source",), ("$source",)),
            state="claimed",
        )
        runner = unwrap_extracted_collaborator(_bot(tmp_path)._response_runner)
        with (
            patch.object(
                runner.deps.knowledge_access,
                "resolve_for_agent_async",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(knowledge=None),
            ),
            patch("mindroom.approval_execution.create_agent", return_value=actor),
            patch("mindroom.approval_execution.typing_indicator", _noop_typing),
            patch("mindroom.approval_execution.close_agent_runtime_state_dbs"),
        ):
            result = await runner._approval_execution.continue_run(
                continuation,
                execution_identity=identity,
                tool_dispatch=ToolDispatchContext(execution_identity=identity),
                decisions={tool_call_id: False},
                denial_reasons={tool_call_id: None},
                tool_trace_collector=[],
                typing_log_context={},
            )
    else:
        config = _build_test_config()
        members = ResolvedExactTeamMembers(
            requested_agent_names=[],
            agents=[],
            display_names=[],
            materialized_agent_names=set(),
            failed_agent_names=[],
        )
        scope = SimpleNamespace(storage=None, storage_factory=lambda: actor.db)
        with (
            patch("mindroom.teams.materialize_exact_team_members", return_value=members),
            patch("mindroom.teams.open_bound_scope_session_context", return_value=nullcontext(scope)),
            patch("mindroom.teams.build_materialized_team_instance", return_value=actor),
            patch("mindroom.teams.close_team_runtime_state_dbs"),
        ):
            result = await continue_paused_team_run(
                member_names=(),
                approval_calls=calls,
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=runtime_paths_for(config),
                execution_identity=identity,
                session_id="session-1",
                run_id=run_id,
                user_id="@user:localhost",
                configured_team_name=identity.agent_name,
                model_name="default",
                decisions={tool_call_id: False},
                denial_reasons={tool_call_id: None},
                refresh_scheduler=None,
                prior_presentation_state=_TeamStreamPresentation.new([], [], show_tool_calls=True).to_state(),
            )

    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["agent", "team"])
@pytest.mark.parametrize("disable_before_pause", [False, True])
async def test_rebuilt_approval_resumes_latest_native_policy(
    tmp_path: Path,
    entity: str,
    *,
    disable_before_pause: bool,
) -> None:
    """Losing settings on rebuild replays the full prefix instead of the checkpoint."""
    requests: list[dict[str, Any]] = []
    executed: list[str] = []

    def lookup() -> str:
        executed.append("lookup")
        return "Port 4321"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("stream"):
            events = _event("response.output_text.delta", delta="Ready", output_index=0, content_index=0)
            events += _event("response.completed", response=_response([_ANSWER]))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)
        return httpx.Response(200, json=_response([_CHECKPOINT, _ANSWER] if len(requests) == 1 else [_CALL]))

    name = "general" if entity == "agent" else "research"
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=name,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    db = SqliteDb(db_file=str(tmp_path / "approval.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="test-key", http_client=http_client)

        def build_actor(*, threshold: int | None) -> Agent | Team:
            model = MindRoomOpenAIResponses(id="gpt-6-astra", async_client=client, store=False)
            model.configure_native_compaction(threshold=threshold)
            kwargs = {
                "id": name,
                "model": model,
                "db": db,
                "tools": [Function(name="lookup", entrypoint=lookup, requires_confirmation=True)],
                "add_history_to_context": True,
                "store_history_messages": False,
                "telemetry": False,
            }
            return Agent(**kwargs) if entity == "agent" else Team(members=[], **kwargs)

        actor = build_actor(threshold=1024)
        await actor.arun("Original canonical transcript", session_id="session-1", user_id="@user:localhost")
        actor = build_actor(threshold=None if disable_before_pause else 1024)
        paused = await actor.arun("Look up the port", session_id="session-1", user_id="@user:localhost")
        requirement = (paused.requirements or [])[0]
        assert requirement.tool_execution is not None
        tool_call_id = requirement.tool_execution.tool_call_id
        assert tool_call_id is not None
        rebuilt = build_actor(threshold=None)

        result = await _resume_approval(
            rebuilt,
            identity=identity,
            run_id=paused.run_id,
            tool_call_id=tool_call_id,
            tmp_path=tmp_path,
        )

        assert isinstance(result, CompletedApprovalRun)
        assert executed == []
        assert len(requests) == 3
        resumed = requests[-1]
        replay = resumed["input"]
        if disable_before_pause:
            assert "context_management" not in resumed
            assert _CHECKPOINT not in replay
            assert "Original canonical transcript" in json.dumps(replay)
        else:
            assert resumed.get("context_management") == [{"type": "compaction", "compact_threshold": 1024}]
            assert _CHECKPOINT in replay
            assert "Original canonical transcript" not in json.dumps(replay)
        assert sum(item.get("type") == "function_call" for item in replay) == 1
        assert sum(item.get("type") == "function_call_output" for item in replay) == 1
        session = await rebuilt.aget_session(session_id="session-1", user_id="@user:localhost")
        assert session is not None
        assert len(session.runs or []) == 2
        assert any(
            message.content == "Original canonical transcript"
            for run in session.runs or []
            for message in run.messages or []
        )
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("entity", ["agent", "team"])
@pytest.mark.parametrize("portable_before_pause", [False, True], ids=["unbounded", "bounded"])
@pytest.mark.parametrize("provenance", ["saved", "legacy", "malformed"])
async def test_rebuilt_approval_preserves_reasoning_context(  # noqa: PLR0915
    tmp_path: Path,
    entity: str,
    provenance: str,
    *,
    portable_before_pause: bool,
) -> None:
    """Approval rebuilds must retain stored continuation or complete canonical reasoning."""
    requests: list[dict[str, Any]] = []
    executed: list[str] = []

    def lookup() -> str:
        executed.append("lookup")
        return "Port 4321"

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json=_response([_REASONING, _CALL]))
        events = _event("response.output_text.delta", delta="Ready", output_index=0, content_index=0)
        events += _event("response.completed", response=_response([_ANSWER]))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=events)

    name = "general" if entity == "agent" else "research"
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=name,
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    db = SqliteDb(db_file=str(tmp_path / "approval.db"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        client = AsyncOpenAI(api_key="test-key", http_client=http_client)

        def build_actor(*, portable: bool) -> Agent | Team:
            model = MindRoomOpenAIResponses(
                id="gpt-6-astra",
                async_client=client,
                store=True,
                include=["reasoning.encrypted_content"],
            )
            model.configure_portable_replay(enabled=portable)
            kwargs = {
                "id": name,
                "model": model,
                "db": db,
                "tools": [Function(name="lookup", entrypoint=lookup, requires_confirmation=True)],
                "add_history_to_context": True,
                "store_history_messages": False,
                "telemetry": False,
            }
            return Agent(**kwargs) if entity == "agent" else Team(members=[], **kwargs)

        actor = build_actor(portable=portable_before_pause)
        paused = await actor.arun("Look up the port", session_id="session-1", user_id="@user:localhost")
        requirement = (paused.requirements or [])[0]
        assert requirement.tool_execution is not None
        tool_call_id = requirement.tool_execution.tool_call_id
        assert tool_call_id is not None
        if provenance != "saved":
            session = await actor.aget_session(session_id="session-1", user_id="@user:localhost")
            assert session is not None
            persisted = session.get_run(paused.run_id)
            assert persisted is not None
            latest = next(message for message in reversed(persisted.messages or []) if message.role == "assistant")
            assert latest.provider_data is not None
            latest.provider_data.pop("mindroom_portable_replay", None)
            if provenance == "malformed":
                latest.provider_data["mindroom_portable_replay"] = "false"
            db.upsert_session(session)
        result = await _resume_approval(
            build_actor(portable=not portable_before_pause),
            identity=identity,
            run_id=paused.run_id,
            tool_call_id=tool_call_id,
            tmp_path=tmp_path,
        )
        assert isinstance(result, CompletedApprovalRun)
        assert executed == []
        assert len(requests) == 2
        resumed = requests[-1]
        assert resumed["store"] is True
        assert sum(item.get("type") == "function_call_output" for item in resumed["input"]) == 1
        if portable_before_pause:
            assert "previous_response_id" not in resumed
            assert _REASONING in resumed["input"]
            assert sum(item.get("type") == "function_call" for item in resumed["input"]) == 1
        else:
            assert resumed["previous_response_id"] == "resp_done"
            assert not any(item.get("type") in {"reasoning", "function_call"} for item in resumed["input"])
    db.close()
