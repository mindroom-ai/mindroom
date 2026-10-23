"""OpenAI-compatible chat completions API for MindRoom agents.

Exposes MindRoom agents as an OpenAI-compatible API so any chat frontend
(LibreChat, Open WebUI, LobeChat, etc.) can use them as selectable "models".

Route handlers orchestrate: parse (``openai_request_parsing``) ->
resolve identity -> core execution seam (``ai.py`` / ``teams/envelope.py``) ->
protocol formatting (``openai_streaming_protocol``).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import weakref
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictStr, TypeAdapter
from starlette.background import BackgroundTask

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_run_context import prepend_knowledge_availability_notice
from mindroom.ai import AIStreamChunk, ResponseTurnContext, ai_response, stream_agent_response
from mindroom.api import config_lifecycle
from mindroom.api.open_access import open_access_rejection
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
from mindroom.api.response_activity import track_openai_request
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.budgets.monitor import budget_model
from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.constants import AI_RUN_METADATA_KEY, ROUTER_AGENT_NAME, RuntimePaths, runtime_env_flag
from mindroom.helper_usage import helper_usage_context
from mindroom.history.session_context import ScopeSessionContext, open_bound_scope_session_context
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access_async
from mindroom.llm.events import ContentDelta, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.request_context import RequestAttribution, bind_llm_request_log_context
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.response_activity import ResponseIdentity  # noqa: TC001 - FastAPI evaluates dependency annotations.
from mindroom.response_turn import build_llm_request_log_context, stream_with_llm_request_log_context
from mindroom.routing import suggest_responder
from mindroom.teams.envelope import prepare_materialized_team_execution, run_prepared_team
from mindroom.teams.members import (
    BuiltTeam,
    ResolvedExactTeamMembers,
    TeamTurnModelSelection,
    build_team_spec,
    materialize_exact_team_members,
    team_label,
)
from mindroom.teams.presentation import format_team_response
from mindroom.teams.runtime import MemberEvent, team_usage_runs
from mindroom.teams.spec import TeamMode
from mindroom.teams.storage import TeamRunRecords
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.tool_system.runtime_context import DetachedRequesterContext, detached_requester_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
    stream_with_tool_execution_identity,
    tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence

    from mindroom.api.openai_request_parsing import ChatCompletionRequest
    from mindroom.api.openai_streaming_protocol import ToolStreamState
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.llm.runs import RunRecord, TeamRunRecord
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.teams.envelope import PreparedTeamExecution
    from mindroom.teams.runtime import TeamRunEvent

logger = get_logger(__name__)


router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])

# Per-session completion locks keep same-session /v1 completions from
# interleaving session writes and post-response history compaction.
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


async def _finish_openai_completion(completion_lock: asyncio.Lock, budget_monitor: BudgetMonitor | None) -> None:
    """Release the session lock and let budgets count the spend of the finished completion."""
    _release_openai_completion_lock(completion_lock)
    if budget_monitor is not None:
        budget_monitor.response_finished()


def _attach_openai_completion_lock_release(
    response: JSONResponse | StreamingResponse,
    completion_lock: asyncio.Lock,
    budget_monitor: BudgetMonitor | None,
) -> JSONResponse | StreamingResponse:
    if not isinstance(response, (_OpenAIJSONResponse, _OpenAIStreamingResponse)):
        _release_openai_completion_lock(completion_lock)
        msg = f"OpenAI completion response must use a finalizer-safe response class, got {type(response).__name__}"
        raise TypeError(msg)
    response.always_background = BackgroundTask(_finish_openai_completion, completion_lock, budget_monitor)
    return response


def _openai_team_request_log_context(
    *,
    team_name: str,
    session_id: str,
    requester_id: str | None,
    prepared: PreparedTeamExecution,
) -> dict[str, object]:
    metadata = prepared.run_metadata
    correlation_id = metadata.get("correlation_id") if metadata is not None else None
    return build_llm_request_log_context(
        RequestAttribution(
            agent_id=f"team/{team_name}",
            session_id=session_id,
            room_id=None,
            thread_id=None,
            reply_to_event_id=None,
            requester_id=requester_id,
            correlation_id=correlation_id if isinstance(correlation_id, str) else uuid4().hex,
        ),
        prompt=prepared.prompt_text,
        model_prompt=None,
        full_prompt=prepared.prompt_text,
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
    """Model token usage of the run behind one response, zero when the run reported none."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def _usage_info(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
    provider: str | None,
    model_id: str | None,
) -> _UsageInfo:
    """Build OpenAI-style usage, where prompt_tokens includes cached input on every provider."""
    prompt_tokens = (
        context_input_tokens_from_counts(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            provider=provider,
            configured_provider=None,
            model_id=model_id,
        )
        or 0
    )
    return _UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=output_tokens,
        total_tokens=prompt_tokens + output_tokens,
    )


def _usage_from_run_metadata(run_metadata: dict[str, Any]) -> _UsageInfo:
    """Read usage from the versioned AI run metadata that an agent run reports."""
    payload = run_metadata.get(AI_RUN_METADATA_KEY, {})
    usage = payload.get("usage") or {}
    model = payload.get("model") or {}
    return _usage_info(
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_tokens"),
        cache_write_tokens=usage.get("cache_write_tokens"),
        provider=model.get("provider"),
        model_id=model.get("id"),
    )


def _usage_from_team_output(record: TeamRunRecord, members: Sequence[RunRecord]) -> _UsageInfo:
    """Sum usage over the runs of a team turn, normalizing each run for its own provider."""
    total = _UsageInfo()
    for run, metrics in team_usage_runs(record, members):
        usage = _usage_info(
            input_tokens=metrics.input_tokens,
            output_tokens=metrics.output_tokens,
            cache_read_tokens=metrics.cache_read_tokens,
            cache_write_tokens=metrics.cache_write_tokens,
            provider=run.model_provider,
            model_id=run.model,
        )
        total = _UsageInfo(
            prompt_tokens=total.prompt_tokens + usage.prompt_tokens,
            completion_tokens=total.completion_tokens + usage.completion_tokens,
            total_tokens=total.total_tokens + usage.total_tokens,
        )
    return total


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


@dataclass(frozen=True, slots=True)
class _AuthenticatedCaller:
    """One authenticated `/v1` caller."""

    # Full digest of the validated key, or "noauth" when no keys are configured.
    session_namespace: str
    requester_id: str | None


def _authenticate_request(  # noqa: PLR0911
    request: Request,
    authorization: str | None,
    runtime_paths: RuntimePaths,
) -> JSONResponse | _AuthenticatedCaller:
    """Authenticate one `/v1` request."""
    keys_env = runtime_paths.env_value("OPENAI_COMPAT_API_KEYS", default="") or ""
    allow_unauthenticated = runtime_env_flag(
        "OPENAI_COMPAT_ALLOW_UNAUTHENTICATED",
        runtime_paths,
        default=False,
    )
    if not keys_env.strip():
        if allow_unauthenticated:
            # A completion runs an agent with its tools, so a rebound or cross-site page must not trigger one.
            rejection = open_access_rejection(request.headers, request.method, runtime_paths)
            if rejection is not None:
                return _error_response(rejection.status_code, rejection.detail, code="permission_denied")
            return _AuthenticatedCaller(session_namespace="noauth", requester_id=None)
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

    requester_id = _api_key_requester(token, runtime_paths)
    if isinstance(requester_id, JSONResponse):
        return requester_id
    return _AuthenticatedCaller(
        session_namespace=hashlib.sha256(token.encode()).hexdigest(),
        requester_id=requester_id,
    )


def _api_key_requester(token: str, runtime_paths: RuntimePaths) -> JSONResponse | str | None:
    """Resolve a validated key's optional canonical requester binding."""
    mappings_env = runtime_paths.env_value("OPENAI_COMPAT_API_KEY_REQUESTERS", default="") or ""
    if not mappings_env.strip():
        return None
    try:
        mappings = TypeAdapter(dict[StrictStr, StrictStr]).validate_json(mappings_env)
        validate_concrete_matrix_user_ids(list(set(mappings.values())), field_name="API requesters")
    except (ValueError, TypeError):
        return _error_response(503, "Invalid API requester configuration", error_type="server_error")
    return mappings.get(token)


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


def _requester_authority(
    request: Request,
    requester_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> DetachedRequesterContext | JSONResponse | None:
    """Resolve authenticated API authority without inventing a Matrix conversation."""
    if requester_id is None:
        return None
    requester_id = resolve_human_requester_alias(requester_id, config, runtime_paths)
    if not is_human_requester_id(requester_id, config, runtime_paths):
        return _error_response(403, "API requester must be a human identity", code="permission_denied")
    trigger_runtime = config_lifecycle.app_state(request.app).external_trigger_runtime

    def current_config() -> Config | None:
        api_state = config_lifecycle.require_api_state(request.app)
        with api_state.config_lock:
            snapshot = api_state.snapshot
            return snapshot.runtime_config if snapshot.runtime_paths == runtime_paths else None

    return DetachedRequesterContext(
        requester_id=requester_id,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=(
            trigger_runtime.agent_reply_memberships if trigger_runtime is not None else AgentReplyMembershipIndex()
        ),
        config_provider=current_config,
        budget_monitor=config_lifecycle.app_state(request.app).budget_monitor,
    )


def _requester_allows_model(model: str, authority: DetachedRequesterContext | None) -> bool:
    """Apply the selected agent's or team's own responder access to a mapped caller.

    A team's access grants its exact member agents for team requests, so members are not checked separately.
    """
    if authority is None:
        return True
    return is_sender_allowed_for_responder(
        authority.requester_id,
        model.removeprefix(TEAM_MODEL_PREFIX),
        None,
        authority.config,
        authority.runtime_paths,
        authority.agent_reply_memberships,
    )


async def _resolve_auto_route(
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    authority: DetachedRequesterContext | None = None,
) -> str | JSONResponse:
    """Resolve auto-routing to a specific agent name.

    Returns the resolved agent name, or a JSONResponse error if no agent is
    available or the judgment explicitly finds no suitable responder.
    """
    available = openai_compatible_agent_names(config)
    if authority is not None:
        available = [name for name in available if _requester_allows_model(name, authority)]
        if not available:
            return _error_response(403, "No agents are authorized for this requester", code="permission_denied")
    if not available:
        return _error_response(
            500,
            "No OpenAI-compatible agents configured for auto-routing",
            error_type="server_error",
        )

    selection = await suggest_responder(prompt, available, config, runtime_paths, thread_history)
    if selection is None:
        routed = available[0]
        logger.warning("Auto-routing failed, falling back", agent=routed)
        return routed
    if selection.entity_name is None:
        return _error_response(
            400,
            "No suitable agent for this request; choose a model explicitly or rephrase the request",
            code="no_suitable_responder",
        )
    logger.info("Auto-routed", requested="auto", resolved=selection.entity_name)
    return selection.entity_name


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
    caller = _authenticate_request(request, authorization, runtime_paths)
    if isinstance(caller, JSONResponse):
        return caller

    config, runtime_paths = _load_config(request, runtime_paths=runtime_paths)

    authority = _requester_authority(request, caller.requester_id, config, runtime_paths)
    if isinstance(authority, JSONResponse):
        return authority

    # Use config file mtime as creation timestamp
    try:
        created = int(runtime_paths.config_path.stat().st_mtime)
    except OSError:
        created = 0

    compatible_agents = set(openai_compatible_agent_names(config))
    compatible_agents = {name for name in compatible_agents if _requester_allows_model(name, authority)}
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
        if not _requester_allows_model(f"{TEAM_MODEL_PREFIX}{team_name}", authority):
            continue
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
async def chat_completions(
    request: Request,
    activity: Annotated[ResponseIdentity, Depends(track_openai_request, scope="request")],
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """Create a chat completion (non-streaming or streaming)."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    caller = _authenticate_request(request, authorization, runtime_paths)
    if isinstance(caller, JSONResponse):
        return caller

    parsed = _parse_chat_request(request, await request.body(), runtime_paths=runtime_paths)
    if isinstance(parsed, JSONResponse):
        return parsed
    req, config, runtime_paths, prompt, thread_history = parsed
    authority = _requester_authority(request, caller.requester_id, config, runtime_paths)
    if isinstance(authority, JSONResponse):
        return authority
    with detached_requester_context(authority):
        response = await _chat_completions(
            request,
            req,
            config,
            runtime_paths,
            prompt,
            thread_history,
            authority,
            activity,
            session_namespace=caller.session_namespace,
        )
    if isinstance(response, StreamingResponse):
        body_iterator = response.body_iterator
        response.body_iterator = context_bound_async_stream(
            context_factory=lambda: detached_requester_context(authority),
            stream_factory=lambda: aiter(body_iterator),
        )
    return response


async def _chat_completions(  # noqa: C901, PLR0912
    request: Request,
    req: ChatCompletionRequest,
    config: Config,
    runtime_paths: RuntimePaths,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    authority: DetachedRequesterContext | None,
    activity: ResponseIdentity,
    *,
    session_namespace: str,
) -> JSONResponse | StreamingResponse:
    """Execute a completion inside its authenticated requester boundary."""
    # Resolve auto-routing if model is "auto"
    agent_name = req.model
    if agent_name == AUTO_MODEL_NAME:
        result = await _resolve_auto_route(
            prompt,
            config,
            runtime_paths,
            thread_history,
            authority=authority,
        )
        if isinstance(result, JSONResponse):
            return result
        agent_name = result

    if not _requester_allows_model(agent_name, authority):
        return _error_response(403, "This requester is not authorized for the model", code="permission_denied")

    activity.responder = agent_name
    activity.requester_id = authority.requester_id if authority is not None else None

    # Derive a key-namespaced session ID from request headers or fallback UUID.
    session_id = _derive_session_id(agent_name, request, session_namespace)
    if authority is not None:
        requester_digest = hashlib.sha256(authority.requester_id.encode()).hexdigest()
        session_id = f"requester:{requester_digest}:{session_id}"
    logger.info(
        "Chat completion request",
        model=agent_name,
        stream=req.stream,
        session_id=session_id,
        requester_id=authority.requester_id if authority is not None else None,
    )
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        requester_id=authority.requester_id if authority is not None else None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )
    knowledge_refresh_scheduler = _request_knowledge_refresh_scheduler(request)
    budget_monitor = config_lifecycle.app_state(request.app).budget_monitor
    budget = partial(budget_model, config, runtime_paths, budget_monitor, execution_identity.requester_id)
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
            team_models = _openai_team_models(team_name, config, budget)
            if req.stream:
                response: JSONResponse | StreamingResponse = await _stream_team_completion(
                    team_name,
                    agent_name,
                    prompt,
                    session_id,
                    config,
                    runtime_paths,
                    thread_history,
                    team_models,
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
                        team_models,
                        execution_identity=execution_identity,
                        refresh_scheduler=knowledge_refresh_scheduler,
                    )
        else:
            # Resolve knowledge base for this agent
            try:
                knowledge_resolution = await resolve_agent_knowledge_access_async(
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
            model_name = budget(config.resolve_runtime_model(entity_name=agent_name).model_name)
            if req.stream:
                response = await _stream_completion(
                    agent_name,
                    prompt,
                    session_id,
                    config,
                    runtime_paths,
                    thread_history,
                    knowledge,
                    model_name=model_name,
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
                        knowledge,
                        model_name=model_name,
                        execution_identity=execution_identity,
                        refresh_scheduler=knowledge_refresh_scheduler,
                    )
    except BaseException:
        _release_openai_completion_lock(completion_lock)
        raise

    return _attach_openai_completion_lock_release(response, completion_lock, budget_monitor)


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


def _openai_agent_turn_context(
    agent_name: str,
    *,
    session_id: str,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
) -> ResponseTurnContext:
    """Build the turn context for one OpenAI-compatible agent completion."""
    return ResponseTurnContext(
        entity_label=agent_name,
        session_id=session_id,
        run_id=None,
        correlation_id=uuid4().hex,
        reply_to_event_id=None,
        room_id=None,
        thread_id=None,
        requester_id=execution_identity.requester_id if execution_identity is not None else None,
        matrix_run_metadata=None,
        active_model_name=model_name,
    )


async def _non_stream_completion(
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    knowledge: AgentKnowledge | None = None,
    *,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> JSONResponse:
    """Handle non-streaming chat completion."""
    run_metadata: dict[str, Any] = {}
    response_text = await ai_response(
        _openai_agent_turn_context(
            agent_name,
            session_id=session_id,
            model_name=model_name,
            execution_identity=execution_identity,
        ),
        prompt=prompt,
        runtime_paths=runtime_paths,
        config=config,
        thread_history=thread_history,
        knowledge=knowledge,
        include_interactive_questions=False,
        include_openai_compat_guidance=True,
        show_tool_calls=False,
        run_metadata_collector=run_metadata,
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
        usage=_usage_from_run_metadata(run_metadata),
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
    knowledge: AgentKnowledge | None = None,
    *,
    model_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming chat completion via SSE."""
    stream = cast(
        "AsyncGenerator[AIStreamChunk, None]",
        stream_with_tool_execution_identity(
            execution_identity,
            stream_factory=lambda: stream_agent_response(
                _openai_agent_turn_context(
                    agent_name,
                    session_id=session_id,
                    model_name=model_name,
                    execution_identity=execution_identity,
                ),
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=config,
                thread_history=thread_history,
                knowledge=knowledge,
                include_interactive_questions=False,
                include_openai_compat_guidance=True,
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
            if isinstance(first_event, RunFinished):
                content = first_event.record.content
                completed_body = str(content) if content is not None else None
            else:
                text = extract_stream_text(first_event, state.tool_state)
                if text:
                    if isinstance(first_event, ContentDelta | str):
                        saw_text_delta = True
                    yield sse_chunk(state, {"content": text})

            # 3. Stream remaining content
            # Error strings after the first event are sent as content chunks
            # since we can't switch to an error HTTP status mid-stream.
            async for event in stream:
                if isinstance(event, RunFinished):
                    content = event.record.content
                    completed_body = str(content) if content is not None else completed_body
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
                    if isinstance(event, ContentDelta | str):
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


def _openai_team_models(team_name: str, config: Config, budget: Callable[[str], str]) -> TeamTurnModelSelection:
    """Freeze the coordinator and member models of one team completion under the requester's budget."""
    team_config = config.teams[team_name]
    return TeamTurnModelSelection(
        team_model_name=budget(team_config.model or "default"),
        member_model_names={
            member: budget(config.resolve_runtime_model(entity_name=member).model_name) for member in team_config.agents
        },
    )


def _build_team(
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    scope_context: ScopeSessionContext | None = None,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    *,
    models: TeamTurnModelSelection,
) -> tuple[ResolvedExactTeamMembers, BuiltTeam, TeamMode]:
    """Create member builds and build one team for a configured team.

    Raises when the configured team cannot be materialized.
    """
    team_config = config.teams[team_name]
    mode = TeamMode(team_config.mode)
    config.assert_team_agents_supported(team_config.agents, team_name=team_name)

    team_members = materialize_exact_team_members(
        team_config.agents,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=refresh_scheduler,
        reason_prefix=f"Team '{team_name}'",
        active_model_names=models.member_model_names,
    )
    built = build_team_spec(
        members=team_members,
        mode=mode,
        config=config,
        runtime_paths=runtime_paths,
        team_display_name=team_label(team_members.requested_agent_names),
        scope_context=scope_context,
        execution_identity=execution_identity,
        model_name=models.team_model_name,
        configured_team_name=team_name,
    )
    return team_members, built, mode


async def _prepare_openai_team_execution(
    *,
    scope_context: ScopeSessionContext | None,
    team_name: str,
    built: BuiltTeam,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_model_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
) -> PreparedTeamExecution:
    """Prepare one OpenAI-compatible team turn: the scope summary, the replayed history, then the turn."""
    return await prepare_materialized_team_execution(
        ResponseTurnContext(
            entity_label=team_name,
            session_id=None,
            run_id=None,
            correlation_id=uuid4().hex,
            reply_to_event_id=None,
            room_id=None,
            thread_id=None,
            requester_id=execution_identity.requester_id if execution_identity is not None else None,
            matrix_run_metadata=None,
        ),
        scope_context=scope_context,
        spec=built.spec,
        message=prompt,
        thread_history=thread_history,
        config=config,
        runtime_paths=runtime_paths,
        runtime_model=config.resolve_runtime_model(entity_name=team_name, active_model_name=team_model_name),
        response_sender_id=None,
        current_sender_id=None,
        configured_team_name=team_name,
    )


async def _non_stream_team_completion(
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_models: TeamTurnModelSelection,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> JSONResponse:
    """Handle non-streaming team completion."""
    scope_context: ScopeSessionContext | None = None
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    requester_id = execution_identity.requester_id if execution_identity is not None else None
    with open_bound_scope_session_context(
        member_names=[],
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        team_name=team_name,
    ) as opened_scope_context:
        scope_context = opened_scope_context
        try:
            members, built, mode = await asyncio.to_thread(
                _build_team,
                team_name,
                config,
                runtime_paths,
                execution_identity,
                scope_context,
                unavailable_bases,
                refresh_scheduler,
                models=team_models,
            )
        except Exception:
            logger.exception("Team build failed", team=team_name)
            return _error_response(500, "Team execution failed", error_type="server_error")

        logger.info(
            "Team completion request",
            team=team_name,
            mode=mode.value,
            members=len(members.builds),
            session_id=session_id,
        )

        try:
            prompt = prepend_knowledge_availability_notice(
                prompt,
                unavailable_bases,
            )
            prepared = await _prepare_openai_team_execution(
                scope_context=scope_context,
                team_name=team_name,
                built=built,
                prompt=prompt,
                config=config,
                runtime_paths=runtime_paths,
                thread_history=thread_history,
                team_model_name=team_models.team_model_name,
                execution_identity=execution_identity,
            )
        except Exception:
            logger.exception("Team member preparation failed", team=team_name)
            return _error_response(500, "Team execution failed", error_type="server_error")
        records = TeamRunRecords()
        try:
            with (
                bind_llm_request_log_context(
                    **_openai_team_request_log_context(
                        team_name=team_name,
                        session_id=session_id,
                        requester_id=requester_id,
                        prepared=prepared,
                    ),
                ),
                # Helper work during the run, such as a member's Claude Code session, belongs to this conversation.
                helper_usage_context(scope_context),
            ):
                events = run_prepared_team(
                    built,
                    prepared,
                    config=config,
                    runtime_paths=runtime_paths,
                    scope_context=scope_context,
                    team_name=team_name,
                    session_id=session_id,
                    user_id=requester_id,
                    stream=False,
                )
                async with closing_async_stream(events):
                    async for event in events:
                        records.collect(event)
        except Exception:
            logger.exception("Team execution failed", team=team_name)
            return _error_response(500, "Team execution failed", error_type="server_error")
        record = records.terminal(team_name)
        # The member and consensus document of the blocking Matrix reply, as Agno's blocking team reply rendered it on
        # both surfaces; API clients replay this text as history. A run in which nobody wrote anything gives empty
        # text, as it did with Agno, where the Matrix reply falls back to the leader's own messages.
        response_text = format_team_response(record, record.ended_member_runs)

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
            usage=_usage_from_team_output(record, record.ended_member_runs),
        )
        return _OpenAIJSONResponse(content=result.model_dump())


async def _stream_team_completion(  # noqa: C901, PLR0915
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_models: TeamTurnModelSelection,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming team completion via SSE."""
    stack = ExitStack()
    scope_context: ScopeSessionContext | None = None
    stream: AsyncGenerator[TeamRunEvent, None] | None = None
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    requester_id = execution_identity.requester_id if execution_identity is not None else None

    async def _cleanup() -> None:
        if stream is not None:
            await stream.aclose()
        stack.close()

    try:
        scope_context = stack.enter_context(
            open_bound_scope_session_context(
                member_names=[],
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                team_name=team_name,
            ),
        )
        try:
            with tool_execution_identity(execution_identity):
                members, built, mode = await asyncio.to_thread(
                    _build_team,
                    team_name,
                    config,
                    runtime_paths,
                    execution_identity,
                    scope_context,
                    unavailable_bases,
                    refresh_scheduler,
                    models=team_models,
                )
        except Exception:
            logger.exception("Team build failed", team=team_name)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")

        logger.info(
            "Team streaming request",
            team=team_name,
            mode=mode.value,
            members=len(members.builds),
            session_id=session_id,
        )

        try:
            prompt = prepend_knowledge_availability_notice(
                prompt,
                unavailable_bases,
            )
            prepared = await _prepare_openai_team_execution(
                scope_context=scope_context,
                team_name=team_name,
                built=built,
                prompt=prompt,
                config=config,
                runtime_paths=runtime_paths,
                thread_history=thread_history,
                team_model_name=team_models.team_model_name,
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
                requester_id=requester_id,
                prepared=prepared,
            )
            stream = cast(
                "AsyncGenerator[TeamRunEvent, None]",
                stream_with_tool_execution_identity(
                    execution_identity,
                    # Helper work during the run, such as a member's Claude Code session, belongs to this conversation.
                    stream_factory=lambda: context_bound_async_stream(
                        context_factory=lambda: helper_usage_context(scope_context),
                        stream_factory=lambda: stream_with_llm_request_log_context(
                            run_prepared_team(
                                built,
                                prepared,
                                config=config,
                                runtime_paths=runtime_paths,
                                scope_context=scope_context,
                                team_name=team_name,
                                session_id=session_id,
                                user_id=requester_id,
                                stream=True,
                            ),
                            request_context=request_log_context,
                        ),
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
        raise
    else:
        return response


def _team_stream_text(event: TeamRunEvent, tool_state: ToolStreamState) -> str | None:
    """Return the SSE text of one team event: the leader's reply text and every tool call, the leader's or a member's.

    Member text is left out: parallel members would interleave, and the leader's answer carries their results.
    """
    inner = event.event if isinstance(event, MemberEvent) else event
    if isinstance(inner, ToolStarted | ToolCompleted):
        return format_stream_tool_event(inner, tool_state)
    return event.text if isinstance(event, ContentDelta) else None


async def _team_stream_event_generator(
    *,
    stream: AsyncIterator[TeamRunEvent],
    first_event: TeamRunEvent,
    state: CompletionStreamState,
    team_name: str,
    mark_stream_failed: Callable[[], None],
) -> AsyncIterator[str]:
    """Yield the SSE chunks of one team stream, starting with ``first_event``, which the caller already pulled.

    A run that fails mid-stream closes its started tool calls as interrupted and ends with a failure chunk, since
    the HTTP status is already sent.
    """
    tool_state = state.tool_state

    def _chunk(content: str) -> str:
        return sse_chunk(state, {"content": content})

    yield sse_chunk(state, {"role": "assistant"})
    text = _team_stream_text(first_event, tool_state)
    if text:
        yield _chunk(text)

    failed = False
    try:
        async for event in stream:
            text = _team_stream_text(event, tool_state)
            if text:
                yield _chunk(text)
    except Exception:
        logger.exception("Team stream failed during iteration", team=team_name)
        mark_stream_failed()
        failed = True

    # Tool calls that started but never completed, such as those a pause left waiting.
    pending = finalize_pending_tools(tool_state)
    if pending:
        yield _chunk(pending)
    if failed:
        yield _chunk("Team execution failed.")

    logger.info("Team completion sent", team=team_name, stream=True)
    yield sse_chunk(state, {}, finish_reason="stop")
    yield SSE_DONE
