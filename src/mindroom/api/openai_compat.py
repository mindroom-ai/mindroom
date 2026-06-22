"""OpenAI-compatible chat completions API for MindRoom agents.

Exposes MindRoom agents as an OpenAI-compatible API so any chat frontend
(LibreChat, Open WebUI, LobeChat, etc.) can use them as selectable "models".

Route handlers orchestrate: parse (``openai_request_parsing``) ->
resolve identity -> core execution seam (``ai.py`` / ``teams.py``) ->
protocol formatting (``openai_streaming_protocol``).
"""

from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, cast
from uuid import uuid4

from agno.run.agent import RunCompletedEvent, RunContentEvent, RunErrorEvent, RunOutput
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from mindroom.agent_run_context import prepend_knowledge_availability_notice
from mindroom.ai import AIStreamChunk, ai_response, stream_agent_response
from mindroom.api import config_lifecycle
from mindroom.api.openai_request_parsing import (
    AUTO_MODEL_NAME,
    RESERVED_MODEL_NAMES,
    TEAM_MODEL_PREFIX,
    openai_compatible_agent_names,
    parse_chat_completion_body,
    validate_chat_request,
)
from mindroom.api.openai_request_parsing import (
    ChatMessage as _ChatMessage,
)
from mindroom.api.openai_request_parsing import (
    convert_messages as _convert_messages,
)
from mindroom.api.openai_request_parsing import (
    derive_session_id as _derive_session_id,
)
from mindroom.api.openai_request_parsing import (
    openai_incompatible_agents as _openai_incompatible_agents,
)
from mindroom.api.openai_streaming_protocol import (
    SSE_DONE,
    CompletionStreamState,
    extract_agent_stream_failure,
    extract_stream_text,
    finalize_pending_tools,
    format_stream_tool_event,
    new_completion_id,
    sse_chunk,
)
from mindroom.api.openai_streaming_protocol import (
    OpenAIJSONResponse as _OpenAIJSONResponse,
)
from mindroom.api.openai_streaming_protocol import (
    OpenAIStreamingResponse as _OpenAIStreamingResponse,
)
from mindroom.api.openai_streaming_protocol import (
    error_response as _error_response,
)
from mindroom.api.openai_streaming_protocol import (
    is_error_response as _is_error_response,
)
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, runtime_env_flag
from mindroom.execution_preparation import render_prepared_team_messages_text
from mindroom.history import ScopeSessionContext, close_team_runtime_state_dbs, open_bound_scope_session_context
from mindroom.knowledge import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access
from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    build_llm_request_log_context,
    stream_with_llm_request_log_context,
)
from mindroom.logging_config import get_logger
from mindroom.routing import suggest_responder
from mindroom.teams import (
    TeamMode,
    build_materialized_team_instance,
    format_team_response,
    is_cancelled_run_output,
    is_errored_run_output,
    materialize_exact_team_members,
    prepare_materialized_team_execution,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
    stream_with_tool_execution_identity,
    tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.knowledge.knowledge import Knowledge
    from agno.run.agent import RunOutputEvent
    from agno.run.team import TeamRunOutputEvent
    from agno.team import Team

    from mindroom.api.openai_request_parsing import ChatCompletionRequest
    from mindroom.api.openai_streaming_protocol import ToolStreamState
    from mindroom.config.main import Config
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)


router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])

# Per-session completion locks keep same-session /v1 completions from
# interleaving Agno session writes and post-response history compaction.
# The mapping holds weak references: every in-flight request keeps a strong
# reference to its lock (locally and via the attached response finalizer),
# so an entry lives exactly as long as some request for that session is
# still running and the cache is bounded by in-flight concurrency.
_OPENAI_COMPLETION_LOCKS: weakref.WeakValueDictionary[tuple[str, str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _openai_completion_lock(
    *,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
) -> asyncio.Lock:
    """Return the shared lock serializing completions for one agent session."""
    key = (str(runtime_paths.storage_root), agent_name, session_id)
    lock = _OPENAI_COMPLETION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _OPENAI_COMPLETION_LOCKS[key] = lock
    return lock


def _release_openai_completion_lock(completion_lock: asyncio.Lock) -> None:
    if completion_lock.locked():
        completion_lock.release()


def _attach_openai_completion_lock_release(
    response: JSONResponse | StreamingResponse,
    completion_lock: asyncio.Lock,
) -> JSONResponse | StreamingResponse:
    if not isinstance(response, (_OpenAIJSONResponse, _OpenAIStreamingResponse)):
        _release_openai_completion_lock(completion_lock)
        msg = f"OpenAI completion response must use a finalizer-safe response class, got {type(response).__name__}"
        raise TypeError(msg)
    response.always_background = BackgroundTask(_release_openai_completion_lock, completion_lock)
    return response


@dataclass(frozen=True, slots=True)
class _PreparedOpenAITeamPrompt:
    """Prepared team prompt plus the run metadata that must reach Agno."""

    prompt: str
    run_metadata: dict[str, object] | None


def _openai_team_request_log_context(
    *,
    team_name: str,
    session_id: str,
    requester_id: str | None,
    prompt: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    correlation_id = metadata.get("correlation_id") if metadata is not None else None
    return build_llm_request_log_context(
        agent_id=f"team/{team_name}",
        session_id=session_id,
        room_id=None,
        thread_id=None,
        reply_to_event_id=None,
        requester_id=requester_id,
        correlation_id=correlation_id if isinstance(correlation_id, str) else uuid4().hex,
        prompt=prompt,
        model_prompt=None,
        full_prompt=prompt,
        metadata=metadata,
    )


def _load_config(
    request: Request,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> tuple[Config, RuntimePaths]:
    """Load the current runtime config and return it with its path."""
    config, committed_runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    if runtime_paths is not None and committed_runtime_paths != runtime_paths:
        logger.info(
            "Using bound request runtime snapshot for OpenAI-compatible config load",
            requested_config_path=str(runtime_paths.config_path),
            committed_config_path=str(committed_runtime_paths.config_path),
        )
    return config, committed_runtime_paths


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class _ChatCompletionChoice(BaseModel):
    """A single choice in a chat completion response."""

    index: int = 0
    message: _ChatMessage
    finish_reason: str = "stop"


class _UsageInfo(BaseModel):
    """Token usage information (always zeros — Agno doesn't expose counts)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class _ChatCompletionResponse(BaseModel):
    """Non-streaming chat completion response."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[_ChatCompletionChoice]
    usage: _UsageInfo = Field(default_factory=_UsageInfo)
    system_fingerprint: str | None = None


class _ModelObject(BaseModel):
    """A model (agent) entry for the /v1/models response."""

    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "mindroom"
    name: str | None = None
    description: str | None = None


class _ModelListResponse(BaseModel):
    """Response for GET /v1/models."""

    object: str = "list"
    data: list[_ModelObject]


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _authenticate_request(
    authorization: str | None,
    runtime_paths: RuntimePaths,
) -> JSONResponse | None:
    """Authenticate one `/v1` request."""
    keys_env = runtime_paths.env_value("OPENAI_COMPAT_API_KEYS", default="") or ""
    allow_unauthenticated = runtime_env_flag(
        "OPENAI_COMPAT_ALLOW_UNAUTHENTICATED",
        runtime_paths,
        default=False,
    )
    if not keys_env.strip():
        if allow_unauthenticated:
            return None
        return _error_response(
            401,
            "OpenAI-compatible API keys are not configured",
            code="invalid_api_key",
        )

    valid_keys = {k.strip() for k in keys_env.split(",") if k.strip()}

    if not authorization or not authorization.startswith("Bearer "):
        return _error_response(
            401,
            "Missing or invalid Authorization header",
            code="invalid_api_key",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if token not in valid_keys:
        return _error_response(401, "Invalid API key", code="invalid_api_key")

    return None


def _parse_chat_request(
    request: Request,
    body: bytes,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> tuple[ChatCompletionRequest, Config, RuntimePaths, str, list[ResolvedVisibleMessage] | None] | JSONResponse:
    """Parse and validate a chat completion request body.

    Returns (request, config, runtime_paths, prompt, thread_history) on success, or a JSONResponse error.
    """
    req = parse_chat_completion_body(body)
    if isinstance(req, JSONResponse):
        return req

    config, runtime_paths = _load_config(request, runtime_paths=runtime_paths)
    validation_error = validate_chat_request(req, config)
    if validation_error:
        return validation_error

    prompt, thread_history = _convert_messages(req.messages)
    if not prompt:
        return _error_response(400, "No user message content found in messages")

    return req, config, runtime_paths, prompt, thread_history


async def _resolve_auto_route(
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
) -> str | JSONResponse:
    """Resolve auto-routing to a specific agent name.

    Returns the resolved agent name, or a JSONResponse error if routing fails
    and no agents are available.
    """
    available = openai_compatible_agent_names(config)
    if not available:
        return _error_response(
            500,
            "No OpenAI-compatible agents configured for auto-routing",
            error_type="server_error",
        )

    routed = await suggest_responder(prompt, available, config, runtime_paths, thread_history)
    if routed is None:
        routed = available[0]
        logger.warning("Auto-routing failed, falling back", agent=routed)
    else:
        logger.info("Auto-routed", requested="auto", resolved=routed)
    return routed


def _request_knowledge_refresh_scheduler(request: Request) -> KnowledgeRefreshScheduler | None:
    """Return the app-scoped background knowledge refresh scheduler, if configured."""
    return config_lifecycle.app_state(request.app).knowledge_refresh_scheduler


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """List available models (agents) in OpenAI format."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    auth_error = _authenticate_request(authorization, runtime_paths)
    if auth_error is not None:
        return auth_error

    config, runtime_paths = _load_config(request, runtime_paths=runtime_paths)

    # Use config file mtime as creation timestamp
    try:
        created = int(runtime_paths.config_path.stat().st_mtime)
    except OSError:
        created = 0

    compatible_agents = set(openai_compatible_agent_names(config))
    models: list[_ModelObject] = []
    if compatible_agents:
        models.append(
            _ModelObject(
                id=AUTO_MODEL_NAME,
                name="Auto",
                description="Automatically routes to the best agent for your message",
                created=created,
            ),
        )
    for agent_name, agent_config in config.agents.items():
        if agent_name == ROUTER_AGENT_NAME or agent_name in RESERVED_MODEL_NAMES or agent_name not in compatible_agents:
            continue
        models.append(
            _ModelObject(
                id=agent_name,
                name=agent_config.display_name,
                description=agent_config.role or None,
                created=created,
            ),
        )

    # Add teams
    for team_name, team_config in (config.teams or {}).items():
        if _openai_incompatible_agents(team_config.agents, config):
            continue
        models.append(
            _ModelObject(
                id=f"{TEAM_MODEL_PREFIX}{team_name}",
                name=team_config.display_name,
                description=team_config.role or None,
                created=created,
            ),
        )

    response = _ModelListResponse(data=models)
    return JSONResponse(content=response.model_dump())


@router.post("/chat/completions", response_model=None)
async def chat_completions(  # noqa: C901, PLR0912
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """Create a chat completion (non-streaming or streaming)."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    auth_error = _authenticate_request(authorization, runtime_paths)
    if auth_error is not None:
        return auth_error

    # Parse and validate request
    parsed = _parse_chat_request(request, await request.body(), runtime_paths=runtime_paths)
    if isinstance(parsed, JSONResponse):
        return parsed
    req, config, runtime_paths, prompt, thread_history = parsed

    # Resolve auto-routing if model is "auto"
    agent_name = req.model
    if agent_name == AUTO_MODEL_NAME:
        result = await _resolve_auto_route(
            prompt,
            config,
            runtime_paths,
            thread_history,
        )
        if isinstance(result, JSONResponse):
            return result
        agent_name = result

    # Derive a namespaced session ID from request headers or fallback UUID.
    session_id = _derive_session_id(agent_name, request)
    logger.info(
        "Chat completion request",
        model=agent_name,
        stream=req.stream,
        session_id=session_id,
    )
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )
    knowledge_refresh_scheduler = _request_knowledge_refresh_scheduler(request)
    completion_lock = _openai_completion_lock(
        runtime_paths=runtime_paths,
        agent_name=agent_name,
        session_id=session_id,
    )
    await completion_lock.acquire()

    try:
        # Team execution path
        if agent_name.startswith(TEAM_MODEL_PREFIX):
            team_name = agent_name.removeprefix(TEAM_MODEL_PREFIX)
            if req.stream:
                response: JSONResponse | StreamingResponse = await _stream_team_completion(
                    team_name,
                    agent_name,
                    prompt,
                    session_id,
                    config,
                    runtime_paths,
                    thread_history,
                    req.user,
                    execution_identity=execution_identity,
                    refresh_scheduler=knowledge_refresh_scheduler,
                )
            else:
                with tool_execution_identity(execution_identity):
                    response = await _non_stream_team_completion(
                        team_name,
                        agent_name,
                        prompt,
                        session_id,
                        config,
                        runtime_paths,
                        thread_history,
                        req.user,
                        execution_identity=execution_identity,
                        refresh_scheduler=knowledge_refresh_scheduler,
                    )
        else:
            # Resolve knowledge base for this agent
            try:
                knowledge_resolution = resolve_agent_knowledge_access(
                    agent_name,
                    config,
                    runtime_paths,
                    refresh_scheduler=knowledge_refresh_scheduler,
                    execution_identity=execution_identity,
                )
            except Exception:
                logger.warning("Knowledge resolution failed, proceeding without knowledge", exc_info=True)
                knowledge = None
                unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
            else:
                knowledge = knowledge_resolution.knowledge
                unavailable_bases = dict(knowledge_resolution.unavailable)
            prompt = prepend_knowledge_availability_notice(prompt, unavailable_bases)
            if req.stream:
                response = await _stream_completion(
                    agent_name,
                    prompt,
                    session_id,
                    config,
                    runtime_paths,
                    thread_history,
                    req.user,
                    knowledge,
                    execution_identity=execution_identity,
                    refresh_scheduler=knowledge_refresh_scheduler,
                )
            else:
                with tool_execution_identity(execution_identity):
                    response = await _non_stream_completion(
                        agent_name,
                        prompt,
                        session_id,
                        config,
                        runtime_paths,
                        thread_history,
                        req.user,
                        knowledge,
                        execution_identity=execution_identity,
                        refresh_scheduler=knowledge_refresh_scheduler,
                    )
    except BaseException:
        _release_openai_completion_lock(completion_lock)
        raise

    return _attach_openai_completion_lock_release(response, completion_lock)


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


async def _non_stream_completion(
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    _user: str | None,
    knowledge: Knowledge | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> JSONResponse:
    """Handle non-streaming chat completion."""
    response_text = await ai_response(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        thread_history=thread_history,
        room_id=None,
        knowledge=knowledge,
        user_id=None,
        include_interactive_questions=False,
        include_openai_compat_guidance=True,
        active_event_ids=set(),
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
    )

    # Detect error responses from ai_response()
    if _is_error_response(response_text):
        logger.warning("AI response returned error", model=agent_name, session_id=session_id, error=response_text)
        return _error_response(500, "Agent execution failed", error_type="server_error")

    logger.info("Chat completion sent", model=agent_name, stream=False, session_id=session_id)
    response = _ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=agent_name,
        choices=[
            _ChatCompletionChoice(
                message=_ChatMessage(role="assistant", content=response_text),
            ),
        ],
    )
    return _OpenAIJSONResponse(content=response.model_dump())


# ---------------------------------------------------------------------------
# Streaming completion
# ---------------------------------------------------------------------------


async def _stream_completion(  # noqa: C901, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    _user: str | None,
    knowledge: Knowledge | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming chat completion via SSE."""
    stream = cast(
        "AsyncGenerator[AIStreamChunk, None]",
        stream_with_tool_execution_identity(
            execution_identity,
            stream_factory=lambda: stream_agent_response(
                agent_name=agent_name,
                prompt=prompt,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                thread_history=thread_history,
                room_id=None,
                knowledge=knowledge,
                user_id=None,
                include_interactive_questions=False,
                include_openai_compat_guidance=True,
                active_event_ids=set(),
                execution_identity=execution_identity,
                refresh_scheduler=refresh_scheduler,
            ),
        ),
    )

    # Peek at first event to detect errors before committing to SSE
    first_event = await anext(aiter(stream), None)
    if first_event is None:
        await stream.aclose()
        return _error_response(500, "Agent returned empty response", error_type="server_error")

    first_error = extract_agent_stream_failure(first_event)
    if first_error is not None:
        logger.warning(
            "Stream returned error",
            model=agent_name,
            session_id=session_id,
            error=first_error,
        )
        await stream.aclose()
        return _error_response(500, "Agent execution failed", error_type="server_error")

    state = CompletionStreamState.begin(agent_name)
    stream_completed = False
    stream_failed = False

    async def event_generator() -> AsyncIterator[str]:
        nonlocal stream_completed, stream_failed
        saw_text_delta = False
        completed_body: str | None = None
        try:
            # 1. Initial role announcement
            yield sse_chunk(state, {"role": "assistant"})

            # 2. Yield the peeked first event
            if isinstance(first_event, RunCompletedEvent):
                completed_body = str(first_event.content) if first_event.content is not None else None
            else:
                text = extract_stream_text(first_event, state.tool_state)
                if text:
                    if isinstance(first_event, (RunContentEvent, str)):
                        saw_text_delta = True
                    yield sse_chunk(state, {"content": text})

            # 3. Stream remaining content
            # Error strings after the first event are sent as content chunks
            # since we can't switch to an error HTTP status mid-stream.
            async for event in stream:
                if isinstance(event, RunCompletedEvent):
                    completed_body = str(event.content) if event.content is not None else completed_body
                    continue
                failure_text = extract_agent_stream_failure(event)
                if failure_text is not None:
                    stream_failed = True
                    logger.warning(
                        "Stream emitted terminal failure",
                        model=agent_name,
                        session_id=session_id,
                        error=failure_text,
                    )
                    yield sse_chunk(state, {"content": failure_text})
                    break
                text = extract_stream_text(event, state.tool_state)
                if text:
                    if isinstance(event, (RunContentEvent, str)):
                        saw_text_delta = True
                    yield sse_chunk(state, {"content": text})

            if completed_body and not saw_text_delta and not stream_failed:
                yield sse_chunk(state, {"content": completed_body})

            # 4. Final chunk with finish_reason
            logger.info("Chat completion sent", model=agent_name, stream=True)
            yield sse_chunk(state, {}, finish_reason="stop")

            # 5. Stream terminator
            yield SSE_DONE
            stream_completed = not stream_failed
        finally:
            await stream.aclose()

    response = _OpenAIStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )
    response.completion_predicate = lambda: stream_completed
    return response


# ---------------------------------------------------------------------------
# Team completion
# ---------------------------------------------------------------------------


def _build_team(
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    scope_context: ScopeSessionContext | None = None,
    session_id: str | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> tuple[list[Agent], Team, TeamMode]:
    """Create member agents and build one agno.Team for a configured team.

    Raises when the configured team cannot be materialized.
    """
    team_config = config.teams[team_name]
    mode = TeamMode(team_config.mode)
    model_name = team_config.model or "default"
    config.assert_team_agents_supported(team_config.agents, team_name=team_name)

    team_members = materialize_exact_team_members(
        team_config.agents,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        session_id=session_id,
        include_openai_compat_guidance=True,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=refresh_scheduler,
        reason_prefix=f"Team '{team_name}'",
    )
    try:
        team = build_materialized_team_instance(
            requested_agent_names=team_members.requested_agent_names,
            agents=team_members.agents,
            mode=mode,
            config=config,
            runtime_paths=runtime_paths,
            model_name=model_name,
            configured_team_name=team_name,
            scope_context=scope_context,
            execution_identity=execution_identity,
        )
    except Exception:
        close_team_runtime_state_dbs(
            agents=team_members.agents,
            team_db=None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
        raise
    return team_members.agents, team, mode


def _format_team_output(response: TeamRunOutput | RunOutput) -> str:
    """Format a TeamRunOutput into a single string for the API response."""
    parts = format_team_response(response)
    return "\n\n".join(parts) if parts else str(response.content or "")


def _is_failed_team_output(response: TeamRunOutput | RunOutput) -> bool:
    """Return whether a fallback team output ended in a terminal non-success state."""
    return is_errored_run_output(response) or is_cancelled_run_output(response)


async def _prepare_openai_team_prompt(
    *,
    scope_context: ScopeSessionContext | None,
    team_name: str,
    agents: list[Agent],
    team: Team,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> _PreparedOpenAITeamPrompt:
    """Prepare the final prompt for one OpenAI-compatible team run."""
    prepared_execution = await prepare_materialized_team_execution(
        scope_context=scope_context,
        agents=agents,
        team=team,
        message=prompt,
        thread_history=thread_history,
        config=config,
        runtime_paths=runtime_paths,
        active_model_name=config.resolve_runtime_model(entity_name=team_name).model_name,
        reply_to_event_id=None,
        active_event_ids=frozenset(),
        response_sender_id=None,
        current_sender_id=None,
        room_id=None,
        thread_id=None,
        requester_id=execution_identity.requester_id if execution_identity is not None else None,
        correlation_id=uuid4().hex,
        compaction_outcomes_collector=None,
        configured_team_name=team_name,
        matrix_run_metadata=None,
    )
    return _PreparedOpenAITeamPrompt(
        prompt=render_prepared_team_messages_text(prepared_execution.messages),
        run_metadata=cast("dict[str, object] | None", prepared_execution.run_metadata),
    )


async def _non_stream_team_completion(
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> JSONResponse:
    """Handle non-streaming team completion."""
    agents: list[Agent] = []
    team: Team | None = None
    scope_context: ScopeSessionContext | None = None
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    try:
        with open_bound_scope_session_context(
            agents=[],
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            team_name=team_name,
        ) as opened_scope_context:
            scope_context = opened_scope_context
            try:
                agents, team, mode = await asyncio.to_thread(
                    _build_team,
                    team_name,
                    config,
                    runtime_paths,
                    execution_identity,
                    scope_context,
                    session_id,
                    unavailable_bases,
                    refresh_scheduler,
                )
            except Exception:
                logger.exception("Team build failed", team=team_name)
                return _error_response(500, "Team execution failed", error_type="server_error")

            logger.info(
                "Team completion request",
                team=team_name,
                mode=mode.value,
                members=len(agents),
                session_id=session_id,
            )

            try:
                prompt = prepend_knowledge_availability_notice(
                    prompt,
                    unavailable_bases,
                )
                prepared_team_run = await _prepare_openai_team_prompt(
                    scope_context=scope_context,
                    team_name=team_name,
                    agents=agents,
                    team=team,
                    prompt=prompt,
                    config=config,
                    runtime_paths=runtime_paths,
                    thread_history=thread_history,
                    execution_identity=execution_identity,
                )
            except Exception:
                logger.exception("Team member preparation failed", team=team_name)
                return _error_response(500, "Team execution failed", error_type="server_error")
            try:
                with bind_llm_request_log_context(
                    **_openai_team_request_log_context(
                        team_name=team_name,
                        session_id=session_id,
                        requester_id=execution_identity.requester_id if execution_identity else None,
                        prompt=prepared_team_run.prompt,
                        metadata=prepared_team_run.run_metadata,
                    ),
                ):
                    response = await team.arun(
                        prepared_team_run.prompt,
                        session_id=session_id,
                        user_id=user,
                        metadata=prepared_team_run.run_metadata,
                    )
            except Exception:
                logger.exception("Team execution failed", team=team_name)
                return _error_response(500, "Team execution failed", error_type="server_error")
            if isinstance(response, (TeamRunOutput, RunOutput)) and _is_failed_team_output(response):
                logger.warning(
                    "Team response returned terminal failure",
                    team=team_name,
                    error=str(response.content or "Unknown team failure"),
                )
                return _error_response(500, "Team execution failed", error_type="server_error")
            response_text = (
                _format_team_output(response) if isinstance(response, (TeamRunOutput, RunOutput)) else str(response)
            )

            if _is_error_response(response_text):
                logger.warning("Team response returned error", team=team_name, error=response_text)
                return _error_response(500, "Team execution failed", error_type="server_error")

            logger.info("Team completion sent", team=team_name, stream=False)
            result = _ChatCompletionResponse(
                id=new_completion_id(),
                created=int(time.time()),
                model=model_id,
                choices=[
                    _ChatCompletionChoice(
                        message=_ChatMessage(role="assistant", content=response_text),
                    ),
                ],
            )
            return _OpenAIJSONResponse(content=result.model_dump())
    finally:
        close_team_runtime_state_dbs(
            agents=agents,
            team_db=cast("BaseDb | None", team.db) if team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )


async def _stream_team_completion(  # noqa: C901, PLR0915
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming team completion via SSE."""
    stack = ExitStack()
    agents: list[Agent] = []
    team: Team | None = None
    scope_context: ScopeSessionContext | None = None
    stream: AsyncGenerator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput, None] | None = None
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}

    async def _cleanup() -> None:
        if stream is not None:
            await stream.aclose()
        stack.close()
        close_team_runtime_state_dbs(
            agents=agents,
            team_db=cast("BaseDb | None", team.db) if team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )

    try:
        scope_context = stack.enter_context(
            open_bound_scope_session_context(
                agents=[],
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                team_name=team_name,
            ),
        )
        try:
            with tool_execution_identity(execution_identity):
                agents, team, mode = await asyncio.to_thread(
                    _build_team,
                    team_name,
                    config,
                    runtime_paths,
                    execution_identity,
                    scope_context,
                    session_id,
                    unavailable_bases,
                    refresh_scheduler,
                )
        except Exception:
            logger.exception("Team build failed", team=team_name)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")

        logger.info(
            "Team streaming request",
            team=team_name,
            mode=mode.value,
            members=len(agents),
            session_id=session_id,
        )

        try:
            prompt = prepend_knowledge_availability_notice(
                prompt,
                unavailable_bases,
            )
            prepared_team_run = await _prepare_openai_team_prompt(
                scope_context=scope_context,
                team_name=team_name,
                agents=agents,
                team=team,
                prompt=prompt,
                config=config,
                runtime_paths=runtime_paths,
                thread_history=thread_history,
                execution_identity=execution_identity,
            )
        except Exception:
            logger.exception("Team member preparation failed", team=team_name)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")
        try:
            request_log_context = _openai_team_request_log_context(
                team_name=team_name,
                session_id=session_id,
                requester_id=execution_identity.requester_id if execution_identity else None,
                prompt=prepared_team_run.prompt,
                metadata=prepared_team_run.run_metadata,
            )
            stream = cast(
                "AsyncGenerator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput, None]",
                stream_with_tool_execution_identity(
                    execution_identity,
                    stream_factory=lambda: stream_with_llm_request_log_context(
                        cast(
                            "AsyncGenerator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput, None]",
                            team.arun(
                                prepared_team_run.prompt,
                                stream=True,
                                stream_events=True,
                                session_id=session_id,
                                user_id=user,
                                metadata=prepared_team_run.run_metadata,
                            ),
                        ),
                        request_context=request_log_context,
                    ),
                ),
            )
            first_event = await anext(stream, None)
        except Exception:
            logger.exception("Team execution failed", team=team_name)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")

        if first_event is None:
            await _cleanup()
            return _error_response(500, "Team returned empty response", error_type="server_error")
        first_error = _extract_team_stream_failure(first_event)
        if first_error is not None:
            logger.warning("Team streaming returned terminal failure", team=team_name, error=first_error)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")

        state = CompletionStreamState.begin(model_id)
        stream_completed = False
        stream_failed = False

        def mark_stream_failed() -> None:
            nonlocal stream_failed
            stream_failed = True

        async def _event_generator() -> AsyncGenerator[str, None]:
            nonlocal stream_completed
            try:
                async for chunk in _team_stream_event_generator(
                    stream=stream,
                    first_event=first_event,
                    state=state,
                    team_name=team_name,
                    mark_stream_failed=mark_stream_failed,
                ):
                    yield chunk
                    if chunk == SSE_DONE and not stream_failed:
                        stream_completed = True
            finally:
                await _cleanup()

        response = _OpenAIStreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
        )
        response.completion_predicate = lambda: stream_completed and not stream_failed
    except Exception:
        stack.close()
        close_team_runtime_state_dbs(
            agents=agents,
            team_db=cast("BaseDb | None", team.db) if team is not None else None,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
        raise
    else:
        return response


def _extract_team_stream_failure(
    event: RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput,
) -> str | None:
    """Extract explicit terminal-failure text from a team stream event."""
    if isinstance(event, (RunErrorEvent, TeamRunErrorEvent)):
        return str(event.content or "Unknown team error")
    if isinstance(event, TeamRunCancelledEvent):
        return str(event.reason or event.content or "Unknown team failure")
    if isinstance(event, (TeamRunOutput, RunOutput)) and _is_failed_team_output(event):
        formatted_output = _format_team_output(event).strip()
        return formatted_output or "Unknown team failure"
    return None


def _classify_team_event(
    event: RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput,
    tool_state: ToolStreamState,
) -> str | None:
    """Classify a team stream event and return formatted content, or ``None`` to skip.

    Team leader content (``TeamContentEvent``) is streamed directly — it is the
    synthesized answer and never interleaves.
    Member agent content (``RunContentEvent``) is skipped to prevent interleaving
    from parallel members.
    Tool events (agent-level and team-level) are emitted for progress feedback.
    """
    # Some providers fall back to a single final TeamRunOutput or RunOutput in streaming mode.
    if isinstance(event, (TeamRunOutput, RunOutput)):
        formatted_output = _format_team_output(event).strip()
        return formatted_output or None

    # Tool events — emit for progress feedback
    tool_text = format_stream_tool_event(event, tool_state)
    if tool_text is not None:
        return tool_text

    # Team leader content — stream directly (synthesized answer)
    if isinstance(event, TeamContentEvent) and event.content:
        return str(event.content)

    # Everything else (member content, reasoning, memory, hooks, etc.) — skip.
    return None


async def _team_stream_event_generator(
    *,
    stream: AsyncIterator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput],
    first_event: RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput,
    state: CompletionStreamState,
    team_name: str,
    mark_stream_failed: Callable[[], None],
) -> AsyncIterator[str]:
    """Yield SSE chunks for team streaming responses.

    Streams team leader content (``TeamContentEvent``) directly for real-time output.
    Skips member agent content (``RunContentEvent``) to prevent interleaving.
    Emits all tool events (agent-level and team-level) for progress feedback.

    The caller (``_stream_team_completion``) validates the first event via
    ``_extract_team_stream_failure`` before entering this generator, so
    ``first_event`` is guaranteed to be non-error.
    """
    tool_state = state.tool_state

    def _chunk(content: str) -> str:
        return sse_chunk(state, {"content": content})

    # 1. Role announcement
    yield sse_chunk(state, {"role": "assistant"})

    # 2. First event (guaranteed non-error by preflight)
    text = _classify_team_event(first_event, tool_state)
    if text:
        yield _chunk(text)

    # 3. Remaining events
    try:
        async for event in stream:
            if _extract_team_stream_failure(event) is not None:
                logger.warning("Team stream emitted terminal failure", team=team_name)
                mark_stream_failed()
                pending = finalize_pending_tools(tool_state)
                if pending:
                    yield _chunk(pending)
                yield _chunk("Team execution failed.")
                break

            text = _classify_team_event(event, tool_state)
            if text:
                yield _chunk(text)
    except Exception:
        logger.exception("Team stream failed during iteration", team=team_name)
        mark_stream_failed()
        pending = finalize_pending_tools(tool_state)
        if pending:
            yield _chunk(pending)
        yield _chunk("Team execution failed.")

    # 4. Finalize any tool calls that started but never completed
    pending = finalize_pending_tools(tool_state)
    if pending:
        yield _chunk(pending)

    # 5. Finish
    logger.info("Team completion sent", team=team_name, stream=True)
    yield sse_chunk(state, {}, finish_reason="stop")
    yield SSE_DONE
