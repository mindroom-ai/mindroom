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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictStr, TypeAdapter
from starlette.background import BackgroundTask

from mindroom.agent_run_context import prepend_knowledge_availability_notice
from mindroom.ai import AIStreamChunk, ai_response, stream_agent_response
from mindroom.ai_run_metadata import reply_cost_usd
from mindroom.api import config_lifecycle
from mindroom.api.open_access import open_access_rejection
from mindroom.api.openai_request_parsing import (
    AUTO_MODEL_NAME,
    RESERVED_MODEL_NAMES,
    TEAM_MODEL_PREFIX,
    ChatMessage,
    convert_messages,
    derive_session_id,
    openai_compatible_agent_names,
    openai_incompatible_agents,
    parse_chat_completion_body,
    validate_chat_request,
)
from mindroom.api.openai_streaming_protocol import (
    SSE_DONE,
    CompletionStreamState,
    OpenAIJSONResponse,
    OpenAIStreamingResponse,
    error_response,
    extract_stream_text,
    finalize_pending_tools,
    format_stream_tool_event,
    new_completion_id,
    sse_chunk,
)
from mindroom.api.response_activity import track_openai_request
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.budgets.pricing import model_prices, run_cost_usd
from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.config.resolution import entity_timezone
from mindroom.constants import AI_RUN_METADATA_KEY, RuntimePaths
from mindroom.error_handling import needs_provider_key
from mindroom.failure_text import failure_text
from mindroom.helper_usage import helper_usage_context
from mindroom.history.session_context import ScopeSessionContext, load_bound_scope_session
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access_async
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.request_context import RequestAttribution
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.redaction import redact_sensitive_text
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.response_activity import ActiveResponseIdentity  # noqa: TC001 - FastAPI evaluates dependency annotations.
from mindroom.response_turn import (
    ResponseTurnContext,
    TurnSinks,
    build_llm_request_log_context,
    stream_with_llm_request_log_context,
)
from mindroom.routing import suggest_responder
from mindroom.run_storage import TerminalRecord
from mindroom.teams.envelope import prepare_materialized_team_execution, run_prepared_team
from mindroom.teams.members import (
    BuiltTeam,
    build_team_spec,
    resolve_exact_team_members,
)
from mindroom.teams.runtime import MemberEvent, team_usage, team_usage_runs
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.tool_system.events import FailedReply
from mindroom.tool_system.runtime_context import (
    DetachedRequesterContext,
    detached_requester_context,
    detached_tool_scope,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
)
from mindroom.turn_model import (
    ModelScope,
    TeamTurnModelSelection,
    TurnBudget,
    resolve_team_turn_models,
    resolve_turn_model,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    from mindroom.api.openai_request_parsing import ChatCompletionRequest
    from mindroom.api.openai_streaming_protocol import ToolStreamState
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.main import Config
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
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


@dataclass
class _CompletionCost:
    """The USD cost of one completion's turn: zero when it used no priced model, None until known."""

    usd: float | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))


async def _finish_openai_completion(
    completion_lock: asyncio.Lock,
    budget_monitor: BudgetMonitor,
    requester_id: str | None,
    cost: _CompletionCost,
) -> None:
    """Release the session lock and add the finished completion's cost to its requester's spend."""
    _release_openai_completion_lock(completion_lock)
    budget_monitor.response_finished(requester_id, cost.usd, cost.started_at)


def _attach_openai_completion_lock_release(
    response: JSONResponse | StreamingResponse,
    completion_lock: asyncio.Lock,
    budget_monitor: BudgetMonitor,
    requester_id: str | None,
    cost: _CompletionCost,
) -> JSONResponse | StreamingResponse:
    if not isinstance(response, (OpenAIJSONResponse, OpenAIStreamingResponse)):
        _release_openai_completion_lock(completion_lock)
        msg = f"OpenAI completion response must use a finalizer-safe response class, got {type(response).__name__}"
        raise TypeError(msg)
    response.always_background = BackgroundTask(
        _finish_openai_completion,
        completion_lock,
        budget_monitor,
        requester_id,
        cost,
    )
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


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class _ChatCompletionChoice(BaseModel):
    """A single choice in a chat completion response."""

    index: int = 0
    message: ChatMessage
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


def _usage_from_team_output(record: TeamRunRecord) -> _UsageInfo:
    """Sum usage over the runs of a team turn, normalizing each run for its own provider."""
    total = _UsageInfo()
    for run, metrics in team_usage_runs(record):
        usage = _usage_info(
            input_tokens=metrics.tokens.input_tokens,
            output_tokens=metrics.tokens.output_tokens,
            cache_read_tokens=metrics.tokens.cache_read_tokens,
            cache_write_tokens=metrics.tokens.cache_write_tokens,
            provider=run.model_provider,
            model_id=run.model,
        )
        total = _UsageInfo(
            prompt_tokens=total.prompt_tokens + usage.prompt_tokens,
            completion_tokens=total.completion_tokens + usage.completion_tokens,
            total_tokens=total.total_tokens + usage.total_tokens,
        )
    return total


def _team_cost_usd(record: TeamRunRecord | None, config: Config) -> float | None:
    """Return what a team completion's runs cost, priced as a team reply's run metadata prices them.

    The record a team run hands over holds all its usage, so only a run that handed none over has an unknown cost.
    """
    if record is None:
        return None
    cost = run_cost_usd(
        team_usage(record),
        model_provider=record.model_provider,
        model_id=record.model,
        prices=model_prices(config),
    )
    return 0.0 if cost is None else cost


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
    keys_env = runtime_paths.env_value("OPENAI_COMPAT_API_KEYS", default="")
    allow_unauthenticated = runtime_paths.env_flag("OPENAI_COMPAT_ALLOW_UNAUTHENTICATED")
    if not keys_env:
        if allow_unauthenticated:
            # A completion runs an agent with its tools, so a rebound or cross-site page must not trigger one.
            rejection = open_access_rejection(request.headers, request.method, runtime_paths)
            if rejection is not None:
                return error_response(rejection.status_code, rejection.detail, code="permission_denied")
            return _AuthenticatedCaller(session_namespace="noauth", requester_id=None)
        return error_response(
            401,
            "OpenAI-compatible API keys are not configured",
            code="invalid_api_key",
        )

    valid_keys = {k.strip() for k in keys_env.split(",") if k.strip()}

    if not authorization or not authorization.startswith("Bearer "):
        return error_response(
            401,
            "Missing or invalid Authorization header",
            code="invalid_api_key",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if token not in valid_keys:
        return error_response(401, "Invalid API key", code="invalid_api_key")

    requester_id = _api_key_requester(token, runtime_paths)
    if isinstance(requester_id, JSONResponse):
        return requester_id
    return _AuthenticatedCaller(
        session_namespace=hashlib.sha256(token.encode()).hexdigest(),
        requester_id=requester_id,
    )


def _api_key_requester(token: str, runtime_paths: RuntimePaths) -> JSONResponse | str | None:
    """Resolve a validated key's optional canonical requester binding."""
    mappings_env = runtime_paths.env_value("OPENAI_COMPAT_API_KEY_REQUESTERS", default="")
    if not mappings_env:
        return None
    try:
        mappings = TypeAdapter(dict[StrictStr, StrictStr]).validate_json(mappings_env)
        validate_concrete_matrix_user_ids(list(set(mappings.values())), field_name="API requesters")
    except (ValueError, TypeError):
        return error_response(503, "Invalid API requester configuration", error_type="server_error")
    return mappings.get(token)


def _parse_chat_request(
    request: Request,
    body: bytes,
) -> tuple[ChatCompletionRequest, Config, str, list[ResolvedVisibleMessage] | None] | JSONResponse:
    """Parse and validate a chat completion request body.

    Returns (request, config, prompt, thread_history) on success, or a JSONResponse error.
    """
    req = parse_chat_completion_body(body)
    if isinstance(req, JSONResponse):
        return req

    config, _ = config_lifecycle.read_committed_runtime_config(request)
    validation_error = validate_chat_request(req, config)
    if validation_error:
        return validation_error

    prompt, thread_history = convert_messages(req.messages)
    if not prompt:
        return error_response(400, "No user message content found in messages")

    return req, config, prompt, thread_history


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
        return error_response(403, "API requester must be a human identity", code="permission_denied")

    def current_config() -> Config | None:
        api_state = config_lifecycle.require_api_state(request.app)
        with api_state.config_lock:
            snapshot = api_state.snapshot
            return snapshot.runtime_config if snapshot.runtime_paths == runtime_paths else None

    runtime = config_lifecycle.app_state(request.app).runtime
    return DetachedRequesterContext(
        requester_id=requester_id,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=runtime.agent_reply_memberships,
        config_provider=current_config,
        budget_monitor=runtime.budget_monitor,
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
    authority: DetachedRequesterContext | None,
) -> str | JSONResponse:
    """Resolve auto-routing to a specific agent name.

    Returns the resolved agent name, or a JSONResponse error if no agent is
    available or the judgment explicitly finds no suitable responder.
    """
    available = openai_compatible_agent_names(config)
    if authority is not None:
        available = [name for name in available if _requester_allows_model(name, authority)]
        if not available:
            return error_response(403, "No agents are authorized for this requester", code="permission_denied")
    if not available:
        return error_response(
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
        return error_response(
            400,
            "No suitable agent for this request; choose a model explicitly or rephrase the request",
            code="no_suitable_responder",
        )
    logger.info("Auto-routed", requested="auto", resolved=selection.entity_name)
    return selection.entity_name


def _openai_turn_context(
    entity_label: str,
    *,
    session_id: str | None,
    execution_identity: ToolExecutionIdentity,
    model_name: str | None = None,
    prompt: str = "",
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    knowledge: AgentKnowledge | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> ResponseTurnContext:
    """Build the turn context for one OpenAI-compatible completion, which has no Matrix conversation."""
    return ResponseTurnContext(
        entity_label=entity_label,
        session_id=session_id,
        run_id=None,
        correlation_id=uuid4().hex,
        reply_to_event_id=None,
        room_id=None,
        thread_id=None,
        requester_id=execution_identity.requester_id,
        matrix_run_metadata=None,
        active_model_name=model_name,
        prompt=prompt,
        thread_history=thread_history,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        knowledge=knowledge,
        include_interactive_questions=False,
    )


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

    config, _ = config_lifecycle.read_committed_runtime_config(request)

    authority = _requester_authority(request, caller.requester_id, config, runtime_paths)
    if isinstance(authority, JSONResponse):
        return authority

    # Use config file mtime as creation timestamp
    try:
        created = int(runtime_paths.config_path.stat().st_mtime)
    except OSError:
        created = 0

    compatible_agents = [
        name for name in openai_compatible_agent_names(config) if _requester_allows_model(name, authority)
    ]
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
    for agent_name in compatible_agents:
        if agent_name in RESERVED_MODEL_NAMES:
            continue
        agent_config = config.agents[agent_name]
        models.append(
            _ModelObject(
                id=agent_name,
                name=agent_config.display_name,
                description=agent_config.role or None,
                created=created,
            ),
        )

    # Add teams
    for team_name, team_config in config.teams.items():
        if not _requester_allows_model(f"{TEAM_MODEL_PREFIX}{team_name}", authority):
            continue
        if openai_incompatible_agents(team_config.agents, config):
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
    activity: Annotated[ActiveResponseIdentity, Depends(track_openai_request, scope="request")],
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """Create a chat completion (non-streaming or streaming)."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    caller = _authenticate_request(request, authorization, runtime_paths)
    if isinstance(caller, JSONResponse):
        return caller

    parsed = _parse_chat_request(request, await request.body())
    if isinstance(parsed, JSONResponse):
        return parsed
    req, config, prompt, thread_history = parsed
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


async def _chat_completions(
    request: Request,
    req: ChatCompletionRequest,
    config: Config,
    runtime_paths: RuntimePaths,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    authority: DetachedRequesterContext | None,
    activity: ActiveResponseIdentity,
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
        return error_response(403, "This requester is not authorized for the model", code="permission_denied")

    requester_id = authority.requester_id if authority is not None else None
    activity.responder = agent_name
    activity.requester_id = requester_id

    # Derive a key-namespaced session ID from request headers or fallback UUID.
    session_id = derive_session_id(agent_name, request, session_namespace)
    if authority is not None:
        requester_digest = hashlib.sha256(authority.requester_id.encode()).hexdigest()
        session_id = f"requester:{requester_digest}:{session_id}"
    logger.info(
        "Chat completion request",
        model=agent_name,
        stream=req.stream,
        session_id=session_id,
        requester_id=requester_id,
    )
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )
    runtime = config_lifecycle.app_state(request.app).runtime
    knowledge_refresh_scheduler = runtime.knowledge_refresh_scheduler
    budget_monitor = runtime.budget_monitor
    budget = TurnBudget(monitor=budget_monitor, requester_id=execution_identity.requester_id)
    cost = _CompletionCost()
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
            # No room or thread: the team and each member run their authored models, under the requester's budget.
            team_models = resolve_team_turn_models(
                config,
                runtime_paths,
                ModelScope(entity=team_name),
                config.teams[team_name].agents,
                budget,
            )
            team_completion = _stream_team_completion if req.stream else _non_stream_team_completion
            response: JSONResponse | StreamingResponse = await team_completion(
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
                cost=cost,
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
            turn = _openai_turn_context(
                agent_name,
                session_id=session_id,
                model_name=resolve_turn_model(config, runtime_paths, ModelScope(entity=agent_name), budget).model_name,
                execution_identity=execution_identity,
                prompt=prompt,
                thread_history=thread_history,
                knowledge=knowledge,
                refresh_scheduler=knowledge_refresh_scheduler,
            )
            if req.stream:
                response = await _stream_completion(turn, config, runtime_paths, cost)
            else:
                with detached_tool_scope(execution_identity):
                    response = await _non_stream_completion(turn, config, runtime_paths, cost)
    except BaseException:
        _release_openai_completion_lock(completion_lock)
        raise

    return _attach_openai_completion_lock_release(
        response,
        completion_lock,
        budget_monitor,
        execution_identity.requester_id,
        cost,
    )


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


async def _non_stream_completion(
    turn: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
    cost: _CompletionCost,
) -> JSONResponse:
    """Handle non-streaming chat completion: the streamed turn, collected into one answer."""
    agent_name, session_id = turn.entity_label, turn.session_id
    result = await ai_response(replace(turn, show_tool_calls=False), runtime_paths, config)
    cost.usd = reply_cost_usd(result.run_metadata, completed=result.completed is not None)
    failed = result.failed

    # A failed turn completes no attempt, also when it failed after part of its answer, which its text then holds. One
    # that failed for want of a provider key answers with its reply, the setup guidance the user has to read.
    if result.completed is None and not (failed is not None and needs_provider_key(failed.reply.error)):
        # The reply text can hold the answer the turn had started, which is conversation content; log only why it failed.
        logger.warning(
            "AI response failed",
            model=agent_name,
            session_id=session_id,
            error=redact_sensitive_text(failure_text(failed.reply.error)) if failed is not None else None,
        )
        return error_response(500, "Agent execution failed", error_type="server_error")

    logger.info("Chat completion sent", model=agent_name, stream=False, session_id=session_id)
    response = _ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=agent_name,
        choices=[
            _ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=result.text),
            ),
        ],
        usage=_usage_from_run_metadata(result.run_metadata),
    )
    return OpenAIJSONResponse(content=response.model_dump())


# ---------------------------------------------------------------------------
# Streaming completion
# ---------------------------------------------------------------------------


def _stream_failure(event: AIStreamChunk) -> FailedReply | None:
    """Return the failed turn's reply that ends an agent stream, unless it is the missing-key reply.

    That reply is setup guidance the user has to read, so it streams as the answer, as a blocking completion sends it.
    """
    if isinstance(event, FailedReply) and not needs_provider_key(event.error):
        return event
    return None


async def _stream_completion(
    turn: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
    cost: _CompletionCost,
) -> StreamingResponse | JSONResponse:
    """Handle streaming chat completion via SSE."""
    agent_name, session_id = turn.entity_label, turn.session_id
    run_metadata: dict[str, Any] = {}
    stream = cast(
        "AsyncGenerator[AIStreamChunk]",
        context_bound_async_stream(
            context_factory=lambda: detached_tool_scope(turn.execution_identity),
            stream_factory=lambda: stream_agent_response(
                turn,
                runtime_paths,
                config,
                sinks=TurnSinks(run_metadata_collector=run_metadata),
            ),
        ),
    )

    # Peek at first event to detect errors before committing to SSE
    first_event = await anext(aiter(stream), None)
    if first_event is None:
        await stream.aclose()
        return error_response(500, "Agent returned empty response", error_type="server_error")

    if (first_failure := _stream_failure(first_event)) is not None:
        logger.warning(
            "Stream returned error",
            model=agent_name,
            session_id=session_id,
            error=redact_sensitive_text(failure_text(first_failure.error)),
        )
        await stream.aclose()
        return error_response(500, "Agent execution failed", error_type="server_error")

    state = CompletionStreamState.begin(agent_name)
    stream_completed = False

    async def event_generator() -> AsyncIterator[str]:
        nonlocal stream_completed
        saw_text_delta = stream_failed = False
        completed_body: str | None = None
        try:
            yield sse_chunk(state, {"role": "assistant"})
            # The peeked first event, then the rest. A failure after the first event is sent as a content chunk,
            # since the HTTP status is already sent.
            event: AIStreamChunk | None = first_event
            while event is not None:
                if isinstance(event, RunFinished):
                    content = event.record.content
                    completed_body = content if content is not None else completed_body
                elif (failure := _stream_failure(event)) is not None:
                    stream_failed = True
                    logger.warning(
                        "Stream emitted terminal failure",
                        model=agent_name,
                        session_id=session_id,
                        error=redact_sensitive_text(failure_text(failure.error)),
                    )
                    yield sse_chunk(state, {"content": failure.text})
                    break
                elif text := extract_stream_text(event, state.tool_state):
                    if isinstance(event, TextDelta | FailedReply):
                        saw_text_delta = True
                    yield sse_chunk(state, {"content": text})
                event = await anext(stream, None)

            if completed_body and not saw_text_delta and not stream_failed:
                yield sse_chunk(state, {"content": completed_body})
            logger.info("Chat completion sent", model=agent_name, stream=True)
            yield sse_chunk(state, {}, finish_reason="stop")
            yield SSE_DONE
            stream_completed = not stream_failed
        finally:
            await stream.aclose()
            cost.usd = reply_cost_usd(run_metadata, completed=stream_completed)

    response = OpenAIStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
    )
    response.completion_predicate = lambda: stream_completed
    response.close_stream = stream.aclose
    return response


# ---------------------------------------------------------------------------
# Team completion
# ---------------------------------------------------------------------------


def _build_team(
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    scope_context: ScopeSessionContext,
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail],
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    *,
    models: TeamTurnModelSelection,
) -> BuiltTeam:
    """Build one configured team, whose members build on their first delegation.

    Raises when the configured team's members are not supported.
    """
    team_config = config.teams[team_name]
    config.assert_team_agents_supported(team_config.agents, team_name=team_name)

    team_members = resolve_exact_team_members(
        team_config.agents,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        unavailable_bases=unavailable_bases,
        refresh_scheduler=refresh_scheduler,
        active_model_names=models.member_model_names,
    )
    return build_team_spec(
        members=team_members,
        mode=TeamMode(team_config.mode),
        config=config,
        runtime_paths=runtime_paths,
        scope_context=scope_context,
        execution_identity=execution_identity,
        model_name=models.team_model_name,
        configured_team_name=team_name,
        timezone=entity_timezone(config, team_name),
        include_openai_compat_guidance=True,
    )


async def _prepare_openai_team_execution(
    *,
    scope_context: ScopeSessionContext,
    team_name: str,
    built: BuiltTeam,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_model_name: str,
    execution_identity: ToolExecutionIdentity,
) -> PreparedTeamExecution:
    """Prepare one OpenAI-compatible team turn: the scope summary, the replayed history, then the turn."""
    return await prepare_materialized_team_execution(
        _openai_turn_context(team_name, session_id=None, execution_identity=execution_identity),
        scope_context=scope_context,
        built=built,
        message=prompt,
        thread_history=thread_history,
        config=config,
        runtime_paths=runtime_paths,
        runtime_model=resolve_turn_model(
            config,
            runtime_paths,
            ModelScope(entity=team_name, explicit=team_model_name),
            None,
        ),
        response_sender_id=None,
        current_sender_id=None,
    )


async def _prepare_team_turn(
    team_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_models: TeamTurnModelSelection,
    *,
    execution_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    stream: bool,
) -> tuple[ScopeSessionContext, BuiltTeam, PreparedTeamExecution] | JSONResponse:
    """Load the team's session scope, build the team, and prepare its turn, or return the error response a failure gets."""
    scope_context = load_bound_scope_session(
        member_names=[],
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        team_name=team_name,
    )
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    try:
        with detached_tool_scope(execution_identity):
            built = await asyncio.to_thread(
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
        return error_response(500, "Team execution failed", error_type="server_error")

    logger.info(
        "Team completion request",
        team=team_name,
        stream=stream,
        mode=built.spec.mode.value,
        members=len(built.spec.members),
        session_id=session_id,
    )

    try:
        prepared = await _prepare_openai_team_execution(
            scope_context=scope_context,
            team_name=team_name,
            built=built,
            prompt=prepend_knowledge_availability_notice(prompt, unavailable_bases),
            config=config,
            runtime_paths=runtime_paths,
            thread_history=thread_history,
            team_model_name=team_models.team_model_name,
            execution_identity=execution_identity,
        )
    except Exception:
        logger.exception("Team member preparation failed", team=team_name)
        return error_response(500, "Team execution failed", error_type="server_error")
    return scope_context, built, prepared


def _team_completion_events(
    team_name: str,
    turn: tuple[ScopeSessionContext, BuiltTeam, PreparedTeamExecution],
    *,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> AsyncGenerator[TeamRunEvent]:
    """Return one team completion's run events, each pulled as the requester, under its request log and helper usage.

    Helper work during the run, such as a member's Claude Code session, belongs to this conversation.
    """
    scope_context, built, prepared = turn
    requester_id = execution_identity.requester_id
    request_log_context = _openai_team_request_log_context(
        team_name=team_name,
        session_id=session_id,
        requester_id=requester_id,
        prepared=prepared,
    )
    return cast(
        "AsyncGenerator[TeamRunEvent]",
        context_bound_async_stream(
            context_factory=lambda: detached_tool_scope(execution_identity),
            stream_factory=lambda: context_bound_async_stream(
                context_factory=lambda: helper_usage_context(scope_context),
                stream_factory=lambda: stream_with_llm_request_log_context(
                    run_prepared_team(
                        built,
                        prepared,
                        config=config,
                        runtime_paths=runtime_paths,
                        scope_context=scope_context,
                        user_id=requester_id,
                    ),
                    request_context=request_log_context,
                ),
            ),
        ),
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
    execution_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    cost: _CompletionCost,
) -> JSONResponse:
    """Handle non-streaming team completion: the streamed team run, collected into its leader's answer."""
    turn = await _prepare_team_turn(
        team_name,
        prompt,
        session_id,
        config,
        runtime_paths,
        thread_history,
        team_models,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        stream=False,
    )
    if isinstance(turn, JSONResponse):
        return turn
    records = TerminalRecord(TeamRunRecord)
    events = _team_completion_events(
        team_name,
        turn,
        session_id=session_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    try:
        async with closing_async_stream(events):
            async for event in events:
                records.collect(event)
    except Exception:
        logger.exception("Team execution failed", team=team_name)
        return error_response(500, "Team execution failed", error_type="server_error")
    finally:
        cost.usd = _team_cost_usd(records.record, config)
    record = records.terminal(team_name)
    # The leader's answer, the text a streamed team completion sends; members' text reaches the client through it. A
    # failed team run raises above after it hands over its record, so this one's text is never a failure's.
    response_text = record.handover_content or ""

    logger.info("Team completion sent", team=team_name, stream=False)
    result = _ChatCompletionResponse(
        id=new_completion_id(),
        created=int(time.time()),
        model=model_id,
        choices=[
            _ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=response_text),
            ),
        ],
        usage=_usage_from_team_output(record),
    )
    return OpenAIJSONResponse(content=result.model_dump())


async def _stream_team_completion(  # noqa: C901, PLR0915
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    team_models: TeamTurnModelSelection,
    execution_identity: ToolExecutionIdentity,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    cost: _CompletionCost,
) -> StreamingResponse | JSONResponse:
    """Handle streaming team completion via SSE."""
    stream: AsyncGenerator[TeamRunEvent] | None = None
    records = TerminalRecord(TeamRunRecord)

    async def _cleanup() -> None:
        if stream is not None:
            await stream.aclose()

    turn = await _prepare_team_turn(
        team_name,
        prompt,
        session_id,
        config,
        runtime_paths,
        thread_history,
        team_models,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        stream=True,
    )
    if isinstance(turn, JSONResponse):
        return turn
    try:
        stream = _team_completion_events(
            team_name,
            turn,
            session_id=session_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        first_event = await anext(stream, None)
        if isinstance(first_event, RunFinished) and first_event.record.status is RunStatus.error:
            # A run that failed before it streamed anything ends with its record; the next read raises why.
            first_event = await anext(stream, None)
    except Exception:
        logger.exception("Team execution failed", team=team_name)
        await _cleanup()
        return error_response(500, "Team execution failed", error_type="server_error")

    if first_event is None:
        await _cleanup()
        return error_response(500, "Team returned empty response", error_type="server_error")

    state = CompletionStreamState.begin(model_id)
    stream_completed = False

    async def _event_generator() -> AsyncGenerator[str]:
        """Yield the SSE chunks of the team stream, starting with ``first_event``, which was already pulled.

        A run that fails mid-stream closes its started tool calls as interrupted and ends with a failure chunk,
        since the HTTP status is already sent.
        """
        nonlocal stream_completed
        tool_state = state.tool_state
        try:
            yield sse_chunk(state, {"role": "assistant"})
            records.collect(first_event)
            text = _team_stream_text(first_event, tool_state)
            if text:
                yield sse_chunk(state, {"content": text})
            failed = False
            try:
                async for event in stream:
                    records.collect(event)
                    text = _team_stream_text(event, tool_state)
                    if text:
                        yield sse_chunk(state, {"content": text})
            except Exception:
                logger.exception("Team stream failed during iteration", team=team_name)
                failed = True
            # Tool calls that started but never completed, such as those a pause left waiting.
            pending = finalize_pending_tools(tool_state)
            if pending:
                yield sse_chunk(state, {"content": pending})
            if failed:
                yield sse_chunk(state, {"content": "Team execution failed."})
            logger.info("Team completion sent", team=team_name, stream=True)
            yield sse_chunk(state, {}, finish_reason="stop")
            yield SSE_DONE
            stream_completed = not failed
        finally:
            await _cleanup()
            cost.usd = _team_cost_usd(records.record, config)

    response = OpenAIStreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
    )
    response.completion_predicate = lambda: stream_completed
    response.close_stream = _cleanup
    return response


def _team_stream_text(event: TeamRunEvent, tool_state: ToolStreamState) -> str | None:
    """Return the SSE text of one team event: the leader's reply text and every tool call, the leader's or a member's.

    Member text is left out: parallel members would interleave, and the leader's answer carries their results.
    """
    inner = event.event if isinstance(event, MemberEvent) else event
    if isinstance(inner, ToolStarted | ToolCompleted):
        return format_stream_tool_event(inner, tool_state)
    return event.text if isinstance(event, TextDelta) else None
