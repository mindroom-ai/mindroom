"""OpenAI-compatible chat completions API for MindRoom agents.

Exposes MindRoom agents as an OpenAI-compatible API so any chat frontend
(LibreChat, Open WebUI, LobeChat, etc.) can use them as selectable "models".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, cast
from uuid import uuid4

from agno.run.agent import (
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunContentEvent as TeamContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.background import BackgroundTask

from mindroom.agent_run_context import prepend_knowledge_availability_notice
from mindroom.ai import AIStreamChunk, ai_response, build_matrix_run_metadata, stream_agent_response
from mindroom.api import config_lifecycle
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths, runtime_env_flag
from mindroom.execution_preparation import prepare_bound_team_run_context, render_prepared_team_messages_text
from mindroom.history import (
    ScopeSessionContext,
    close_team_runtime_state_dbs,
    open_bound_scope_session_context,
    run_post_response_compaction_check,
)
from mindroom.knowledge import KnowledgeAvailabilityDetail, resolve_agent_knowledge_access
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.routing import suggest_agent
from mindroom.teams import (
    TeamMode,
    TeamOutcome,
    build_materialized_team_instance,
    format_team_response,
    is_cancelled_run_output,
    is_errored_run_output,
    materialize_exact_team_members,
    resolve_configured_team,
)
from mindroom.tool_system.events import format_tool_completed_event, format_tool_started_event
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    build_tool_execution_identity,
    stream_with_tool_execution_identity,
    tool_execution_identity,
)

_AUTO_MODEL_NAME = "auto"
_TEAM_MODEL_PREFIX = "team/"
_RESERVED_MODEL_NAMES = {_AUTO_MODEL_NAME}

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence

    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.knowledge.knowledge import Knowledge
    from agno.models.message import Message
    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutputEvent
    from agno.run.team import TeamRunOutputEvent
    from agno.team import Team
    from starlette.types import Receive, Scope, Send

    from mindroom.config.main import Config
    from mindroom.history import CompactionOutcome, PostResponseCompactionCheck
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])

_OPENAI_COMPAT_SUPPORTED_WORKER_SCOPES: frozenset[WorkerScope | None] = frozenset({None, "shared"})
_OPENAI_COMPLETION_LOCKS: dict[tuple[str, str, str], asyncio.Lock] = {}


@dataclass(slots=True)
class _ToolStreamState:
    """Track per-stream IDs so tool started/completed updates can be reconciled client-side."""

    next_tool_id: int = 1
    tool_ids_by_call_id: dict[str, str] = field(default_factory=dict)


async def _run_openai_response_backgrounds(
    *,
    completed: bool,
    response_error: BaseException | None,
    completion_background: BackgroundTask | None,
    always_background: BackgroundTask | None,
) -> None:
    """Run completion-scoped and always-run OpenAI response backgrounds."""
    background_error: BaseException | None = None
    if completed and completion_background is not None:
        try:
            await completion_background()
        except BaseException as error:
            background_error = error

    finalizer_error: BaseException | None = None
    if always_background is not None:
        try:
            await always_background()
        except BaseException as error:
            finalizer_error = error

    if response_error is not None:
        raise response_error
    if background_error is not None:
        raise background_error
    if finalizer_error is not None:
        raise finalizer_error


class _OpenAIJSONResponse(JSONResponse):
    """JSON response with separate completion-scoped and always-run finalizers."""

    always_background: BackgroundTask | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        completion_background = self.background
        self.background = None
        completed = False
        response_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as error:
            response_error = error
        else:
            completed = True
        await _run_openai_response_backgrounds(
            completed=completed,
            response_error=response_error,
            completion_background=completion_background,
            always_background=self.always_background,
        )


class _OpenAIStreamingResponse(StreamingResponse):
    """Streaming response with completion-aware compaction and always-run finalizers."""

    always_background: BackgroundTask | None = None
    completion_predicate: Callable[[], bool] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        completion_background = self.background
        self.background = None
        completed = False
        response_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as error:
            response_error = error
            completed = self.completion_predicate() if self.completion_predicate is not None else False
        else:
            completed = self.completion_predicate() if self.completion_predicate is not None else True
        await _run_openai_response_backgrounds(
            completed=completed,
            response_error=response_error,
            completion_background=completion_background,
            always_background=self.always_background,
        )


def _openai_completion_lock(
    *,
    runtime_paths: RuntimePaths,
    agent_name: str,
    session_id: str,
) -> asyncio.Lock:
    key = (str(runtime_paths.storage_root), agent_name, session_id)
    lock = _OPENAI_COMPLETION_LOCKS.get(key)
    if lock is not None:
        return lock
    if len(_OPENAI_COMPLETION_LOCKS) >= 100:
        for candidate_key, candidate_lock in list(_OPENAI_COMPLETION_LOCKS.items()):
            if len(_OPENAI_COMPLETION_LOCKS) < 100:
                break
            if not candidate_lock.locked():
                _OPENAI_COMPLETION_LOCKS.pop(candidate_key, None)
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


@dataclass(frozen=True, slots=True)
class _PreparedOpenAIMaterializedTeamExecution:
    """Prepared team execution state needed by the OpenAI-compatible API."""

    messages: tuple[Message, ...]
    run_metadata: dict[str, object] | None
    unseen_event_ids: list[str]
    post_response_compaction_checks: list[PostResponseCompactionCheck] | None = None


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


def _openai_compatible_agent_names(config: Config) -> list[str]:
    delegation_closures: dict[str, frozenset[str]] = {}
    return [
        agent_name
        for agent_name in config.agents
        if agent_name != ROUTER_AGENT_NAME
        and not _openai_incompatible_agent_closure(agent_name, config, delegation_closures=delegation_closures)
    ]


def _openai_incompatible_agents(agent_names: list[str], config: Config) -> list[str]:
    delegation_closures: dict[str, frozenset[str]] = {}
    return [
        agent_name
        for agent_name in agent_names
        if agent_name in config.agents
        and _openai_incompatible_agent_closure(agent_name, config, delegation_closures=delegation_closures)
    ]


def _openai_incompatible_agent_closure(
    agent_name: str,
    config: Config,
    *,
    delegation_closures: dict[str, frozenset[str]],
) -> frozenset[str]:
    """Return the unsupported agents reachable from one /v1 agent request."""
    return frozenset(
        target_name
        for target_name in config.get_agent_delegation_closure(
            agent_name,
            closures=delegation_closures,
        )
        if target_name in config.agents
        and config.get_agent_execution_scope(target_name) not in _OPENAI_COMPAT_SUPPORTED_WORKER_SCOPES
    )


def _unsupported_worker_scope_error(agent_names: list[str], config: Config) -> JSONResponse:
    invalid_agents = ", ".join(agent_names)
    delegation_closures: dict[str, frozenset[str]] = {}
    invalid_scope_agents = {
        target_name
        for agent_name in agent_names
        for target_name in _openai_incompatible_agent_closure(
            agent_name,
            config,
            delegation_closures=delegation_closures,
        )
    }
    invalid_execution_scopes = {
        config.get_agent_execution_scope(agent_name)
        for agent_name in invalid_scope_agents
        if agent_name in config.agents
    }
    has_private_agents = any(
        config.get_agent(agent_name).private is not None
        for agent_name in invalid_scope_agents
        if agent_name in config.agents
    )
    message = (
        "OpenAI-compatible chat completions currently support only shared agents that are "
        "unscoped or configured with worker_scope=shared, including all delegation targets."
    )
    if invalid_execution_scopes & {"user", "user_agent"}:
        message += " Shared agents with worker_scope=user and worker_scope=user_agent are not yet supported on /v1."
    if has_private_agents:
        message += " Requester-private agents configured with private.per are not yet supported on /v1."
    if invalid_scope_agents and set(agent_names) != invalid_scope_agents:
        message += f" Delegation reaches unsupported agents: {', '.join(sorted(invalid_scope_agents))}."

    return _error_response(
        400,
        f"{message} Unsupported agents: {invalid_agents}",
        param="model",
        code="unsupported_worker_scope",
    )


def _validate_team_model_request(team_name: str, config: Config) -> JSONResponse | None:
    if not config.teams or team_name not in config.teams:
        return _error_response(
            404,
            f"Team '{team_name}' not found",
            param="model",
            code="model_not_found",
        )
    invalid_agents = _openai_incompatible_agents(config.teams[team_name].agents, config)
    if invalid_agents:
        return _unsupported_worker_scope_error(invalid_agents, config)
    return None


def _validate_agent_model_request(agent_name: str, config: Config) -> JSONResponse | None:
    if agent_name not in config.agents or agent_name == ROUTER_AGENT_NAME or agent_name in _RESERVED_MODEL_NAMES:
        return _error_response(
            404,
            f"Model '{agent_name}' not found",
            param="model",
            code="model_not_found",
        )
    invalid_agents = _openai_incompatible_agents([agent_name], config)
    if invalid_agents:
        return _unsupported_worker_scope_error(invalid_agents, config)
    return None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class _ChatMessage(BaseModel):
    """A single message in the chat conversation."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict] | None = None


class _ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[_ChatMessage]
    stream: bool = False
    user: str | None = None
    # Accepted but ignored — agent's model config controls these:
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    stop: str | list[str] | None = None
    n: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    response_format: dict | None = None
    tools: list | None = None
    tool_choice: str | dict | None = None
    stream_options: dict | None = None
    logprobs: bool | None = None
    logit_bias: dict | None = None


# --- Non-streaming response models ---


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


# --- Streaming response models ---


class _ChatCompletionChunkChoice(BaseModel):
    """A single choice in a streaming chunk."""

    index: int = 0
    delta: dict
    finish_reason: str | None = None


class _ChatCompletionChunk(BaseModel):
    """A single SSE chunk in a streaming response."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[_ChatCompletionChunkChoice]
    system_fingerprint: str | None = None


# --- Model listing ---


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


# --- Error response ---


class _OpenAIError(BaseModel):
    """OpenAI-compatible error detail."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class _OpenAIErrorResponse(BaseModel):
    """OpenAI-compatible error wrapper."""

    error: _OpenAIError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Return an OpenAI-style error response."""
    body = _OpenAIErrorResponse(
        error=_OpenAIError(message=message, type=error_type, param=param, code=code),
    )
    return _OpenAIJSONResponse(status_code=status_code, content=body.model_dump())


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


def _is_error_response(text: str) -> bool:
    """Detect error strings returned by ai_response() / stream_agent_response().

    Checks for:
    - Emoji-prefixed errors from get_user_friendly_error_message()
    - [agent_name] bracket prefix followed by error emoji
    - Raw provider error strings (e.g. "Error code: 404 - ...")
    - Raw provider JSON error payloads
    """
    error_prefixes = ("❌", "⏱️", "⏰", "⚠️")
    stripped = text.lstrip()
    if not stripped:
        return False

    # Check for [agent_name] prefix followed by error emoji
    if stripped.startswith("["):
        bracket_end = stripped.find("]")
        if bracket_end != -1:
            after_bracket = stripped[bracket_end + 1 :].lstrip()
            return any(after_bracket.startswith(p) for p in error_prefixes)

    if any(stripped.startswith(p) for p in error_prefixes):
        return True

    # Raw provider errors (agno may surface these as response content)
    return _looks_like_raw_provider_error(stripped)


_RAW_PROVIDER_ERROR_PREFIX_RE = re.compile(
    r"^(?:[\w.]+(?:error|exception):\s*)?error\s*code:\s*",
    re.IGNORECASE,
)
_RAW_PROVIDER_JSON_PREFIXES = (
    '{"error":',
    "{'error':",
    '{"type":"error"',
    '{"type": "error"',
    "{'type': 'error'",
)


def _looks_like_raw_provider_error(text: str) -> bool:
    """Detect raw provider error payloads surfaced as text."""
    lowered = text.casefold()
    if _RAW_PROVIDER_ERROR_PREFIX_RE.search(text):
        return True
    # Some providers return error payloads directly as serialized JSON-ish text.
    return lowered.startswith(_RAW_PROVIDER_JSON_PREFIXES)


def _extract_content_text(content: str | list[dict] | None) -> str:
    """Extract text from a message content field.

    Handles string content and multimodal content lists.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Multimodal: concatenate text parts (coerce to str for robustness)
    return " ".join(str(p["text"]) for p in content if isinstance(p, dict) and p.get("type") == "text" and "text" in p)


def _find_last_user_message(
    conversation: list[ResolvedVisibleMessage],
) -> tuple[str, list[ResolvedVisibleMessage] | None] | None:
    """Find the last user message and split into (prompt, thread_history).

    Returns None if no user message exists.
    """
    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i].sender == "user":
            prompt = conversation[i].body
            history = conversation[:i] if i > 0 else None
            return prompt, history
    return None


def _convert_messages(
    messages: list[_ChatMessage],
) -> tuple[str, list[ResolvedVisibleMessage] | None]:
    """Convert OpenAI messages to MindRoom's (prompt, thread_history) format.

    Returns:
        Tuple of (prompt, thread_history).

    """
    system_parts: list[str] = []
    conversation: list[ResolvedVisibleMessage] = []
    synthetic_index = 0

    for msg in messages:
        if msg.role in ("system", "developer"):
            text = _extract_content_text(msg.content)
            if text:
                system_parts.append(text)
        elif msg.role == "tool":
            continue
        elif msg.role in ("user", "assistant"):
            text = _extract_content_text(msg.content)
            if text:
                synthetic_index += 1
                conversation.append(
                    ResolvedVisibleMessage.synthetic(
                        sender=msg.role,
                        body=text,
                        event_id=f"$openai-{synthetic_index}",
                        timestamp=synthetic_index,
                    ),
                )

    system_prompt = "\n\n".join(system_parts) if system_parts else ""

    if not conversation:
        return system_prompt, None

    result = _find_last_user_message(conversation)
    if result is None:
        return system_prompt, None

    prompt, thread_history = result

    if system_prompt:
        prompt = f"{system_prompt}\n\n{prompt}"

    return prompt, thread_history


def _derive_session_id(
    model: str,
    request: Request,
) -> str:
    """Derive a session ID from request headers or content.

    Priority cascade:
    1. X-Session-Id header (namespaced with API key to prevent cross-key collision)
    2. X-LibreChat-Conversation-Id header + model
    3. Random UUID fallback (collision-safe default when no conversation ID is provided)
    """
    # Namespace prefix from API key to prevent session hijack across keys
    auth = request.headers.get("authorization", "")
    key_namespace = hashlib.sha256(auth.encode()).hexdigest()[:8] if auth else "noauth"

    # 1. Explicit session ID (namespaced to prevent cross-key collision)
    session_id = request.headers.get("x-session-id")
    if session_id:
        return f"{key_namespace}:{session_id}"

    # 2. LibreChat conversation ID
    libre_id = request.headers.get("x-librechat-conversation-id")
    if libre_id:
        return f"{key_namespace}:{libre_id}:{model}"

    # 3. Collision-safe fallback for clients that do not provide a conversation ID.
    # This avoids unintended session sharing across unrelated chats.
    return f"{key_namespace}:ephemeral:{uuid4().hex}"


def _validate_chat_request(
    req: _ChatCompletionRequest,
    config: Config,
) -> JSONResponse | None:
    """Validate a chat completion request. Returns error response or None if valid."""
    if not req.messages:
        return _error_response(400, "Messages array is required and must not be empty")

    agent_name = req.model

    if agent_name.startswith(_TEAM_MODEL_PREFIX):
        return _validate_team_model_request(agent_name.removeprefix(_TEAM_MODEL_PREFIX), config)

    if agent_name == _AUTO_MODEL_NAME:
        return None  # auto-routing handled in chat_completions

    return _validate_agent_model_request(agent_name, config)


def _parse_chat_request(
    request: Request,
    body: bytes,
    *,
    runtime_paths: RuntimePaths | None = None,
) -> tuple[_ChatCompletionRequest, Config, RuntimePaths, str, list[ResolvedVisibleMessage] | None] | JSONResponse:
    """Parse and validate a chat completion request body.

    Returns (request, config, runtime_paths, prompt, thread_history) on success, or a JSONResponse error.
    """
    try:
        req = _ChatCompletionRequest(**json.loads(body))
    except (json.JSONDecodeError, ValidationError):
        return _error_response(400, "Invalid request body")

    config, runtime_paths = _load_config(request, runtime_paths=runtime_paths)
    validation_error = _validate_chat_request(req, config)
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
    available = _openai_compatible_agent_names(config)
    if not available:
        return _error_response(
            500,
            "No OpenAI-compatible agents configured for auto-routing",
            error_type="server_error",
        )

    routed = await suggest_agent(prompt, available, config, runtime_paths, thread_history)
    if routed is None:
        routed = available[0]
        logger.warning("Auto-routing failed, falling back", agent=routed)
    else:
        logger.info("Auto-routed", requested="auto", resolved=routed)
    return routed


def _request_knowledge_refresh_scheduler(request: Request) -> KnowledgeRefreshScheduler | None:
    """Return the app-scoped background knowledge refresh scheduler, if configured."""
    return config_lifecycle.app_state(request.app).knowledge_refresh_scheduler


def _log_missing_knowledge_bases(agent_name: str) -> Callable[[list[str]], None]:
    """Build a missing-knowledge callback for one agent name."""
    return lambda missing_base_ids: logger.warning(
        "Knowledge bases not available for agent",
        agent=agent_name,
        knowledge_bases=missing_base_ids,
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
    auth_error = _authenticate_request(authorization, runtime_paths)
    if auth_error is not None:
        return auth_error

    config, runtime_paths = _load_config(request, runtime_paths=runtime_paths)

    # Use config file mtime as creation timestamp
    try:
        created = int(runtime_paths.config_path.stat().st_mtime)
    except OSError:
        created = 0

    compatible_agents = set(_openai_compatible_agent_names(config))
    models: list[_ModelObject] = []
    if compatible_agents:
        models.append(
            _ModelObject(
                id=_AUTO_MODEL_NAME,
                name="Auto",
                description="Automatically routes to the best agent for your message",
                created=created,
            ),
        )
    for agent_name, agent_config in config.agents.items():
        if (
            agent_name == ROUTER_AGENT_NAME
            or agent_name in _RESERVED_MODEL_NAMES
            or agent_name not in compatible_agents
        ):
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
                id=f"{_TEAM_MODEL_PREFIX}{team_name}",
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
    if agent_name == _AUTO_MODEL_NAME:
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
        if agent_name.startswith(_TEAM_MODEL_PREFIX):
            team_name = agent_name.removeprefix(_TEAM_MODEL_PREFIX)
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
                if knowledge_resolution.missing:
                    _log_missing_knowledge_bases(agent_name)(list(knowledge_resolution.missing))
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
    user: str | None,
    knowledge: Knowledge | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> JSONResponse:
    """Handle non-streaming chat completion."""
    post_response_compaction_checks: list[PostResponseCompactionCheck] = []
    response_text = await ai_response(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        thread_history=thread_history,
        room_id=None,
        knowledge=knowledge,
        user_id=user,
        include_interactive_questions=False,
        include_openai_compat_guidance=True,
        active_event_ids=set(),
        execution_identity=execution_identity,
        post_response_compaction_checks_collector=post_response_compaction_checks,
        refresh_scheduler=refresh_scheduler,
    )

    # Detect error responses from ai_response()
    if _is_error_response(response_text):
        logger.warning("AI response returned error", model=agent_name, session_id=session_id, error=response_text)
        return _error_response(500, "Agent execution failed", error_type="server_error")

    logger.info("Chat completion sent", model=agent_name, stream=False, session_id=session_id)
    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    response = _ChatCompletionResponse(
        id=completion_id,
        created=int(time.time()),
        model=agent_name,
        choices=[
            _ChatCompletionChoice(
                message=_ChatMessage(role="assistant", content=response_text),
            ),
        ],
    )
    return _OpenAIJSONResponse(
        content=response.model_dump(),
        background=_openai_post_response_compaction_task(
            post_response_compaction_checks,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        ),
    )


# ---------------------------------------------------------------------------
# Streaming completion
# ---------------------------------------------------------------------------


def _chunk_json(
    completion_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """Build a JSON string for a single SSE chunk."""
    chunk = _ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            _ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason),
        ],
    )
    return chunk.model_dump_json()


def _extract_tool_call_id(tool: ToolExecution) -> str:
    """Extract the required tool call identifier for streaming tool events."""
    tool_call_id = str(tool.tool_call_id).strip()
    if not tool_call_id:
        msg = "Streaming tool events require a non-empty tool_call_id"
        raise ValueError(msg)
    return tool_call_id


def _allocate_next_tool_id(tool_state: _ToolStreamState) -> str:
    tool_id = str(tool_state.next_tool_id)
    tool_state.next_tool_id += 1
    return tool_id


def _raise_unmaterializable_team(team_name: str, reason: str | None) -> NoReturn:
    msg = reason or f"Team '{team_name}' cannot be materialized"
    raise ValueError(msg)


def _resolve_started_tool_id(tool: ToolExecution, tool_state: _ToolStreamState) -> str:
    tool_call_id = _extract_tool_call_id(tool)

    existing_tool_id = tool_state.tool_ids_by_call_id.get(tool_call_id)
    if existing_tool_id is not None:
        return existing_tool_id

    tool_id = _allocate_next_tool_id(tool_state)
    tool_state.tool_ids_by_call_id[tool_call_id] = tool_id
    return tool_id


def _resolve_completed_tool_id(tool: ToolExecution, tool_state: _ToolStreamState) -> str:
    tool_call_id = _extract_tool_call_id(tool)

    existing_tool_id = tool_state.tool_ids_by_call_id.pop(tool_call_id, None)
    if existing_tool_id is not None:
        return existing_tool_id

    return _allocate_next_tool_id(tool_state)


def _inject_tool_metadata(tool_message: str, *, tool_id: str, state: Literal["start", "done"]) -> str:
    return f'<tool id="{tool_id}" state="{state}">{tool_message}</tool>'


def _escape_tool_payload_text(text: str) -> str:
    return escape(text, quote=True)


def _format_openai_tool_call_display(tool_name: str, args_preview: str | None) -> str:
    safe_tool_name = _escape_tool_payload_text(tool_name)
    if not args_preview:
        return f"{safe_tool_name}()"
    return f"{safe_tool_name}({_escape_tool_payload_text(args_preview)})"


def _format_openai_stream_tool_message(
    tool: ToolExecution,
    *,
    completed: bool,
) -> str:
    if completed:
        _, trace = format_tool_completed_event(tool)
    else:
        _, trace = format_tool_started_event(tool)
    if trace is None:
        return ""

    call_display = _format_openai_tool_call_display(trace.tool_name, trace.args_preview)
    if not completed:
        return call_display

    if trace.result_preview is None:
        return f"{call_display}\n"
    return f"{call_display}\n{_escape_tool_payload_text(trace.result_preview)}"


def _format_stream_tool_event(
    event: RunOutputEvent | TeamRunOutputEvent,
    tool_state: _ToolStreamState,
) -> str | None:
    """Format tool events as inline text for the SSE stream with stable IDs."""
    if isinstance(event, (ToolCallStartedEvent, TeamToolCallStartedEvent)):
        tool = event.tool
        if tool is None:
            return None
        tool_msg = _format_openai_stream_tool_message(tool, completed=False)
        tool_id = _resolve_started_tool_id(tool, tool_state)
        state: Literal["start", "done"] = "start"
    elif isinstance(event, (ToolCallCompletedEvent, TeamToolCallCompletedEvent)):
        tool = event.tool
        if tool is None:
            return None
        tool_msg = _format_openai_stream_tool_message(tool, completed=True)
        tool_id = _resolve_completed_tool_id(tool, tool_state)
        state = "done"
    else:
        return None

    if not tool_msg:
        return None
    return _inject_tool_metadata(tool_msg, tool_id=tool_id, state=state)


def _extract_stream_text(event: AIStreamChunk, tool_state: _ToolStreamState) -> str | None:
    """Extract text content from a stream event."""
    if isinstance(event, RunContentEvent) and event.content:
        return str(event.content)
    if isinstance(event, str):
        return event
    return _format_stream_tool_event(event, tool_state)


def _extract_agent_stream_failure(event: AIStreamChunk) -> str | None:
    """Return terminal agent-stream failure text when the chunk represents one."""
    if isinstance(event, RunErrorEvent):
        return str(event.content or "Agent execution failed.")
    if isinstance(event, str) and _is_error_response(event):
        return event
    return None


async def _stream_completion(  # noqa: C901, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user: str | None,
    knowledge: Knowledge | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming chat completion via SSE."""
    post_response_compaction_checks: list[PostResponseCompactionCheck] = []
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
                user_id=user,
                include_interactive_questions=False,
                include_openai_compat_guidance=True,
                active_event_ids=set(),
                execution_identity=execution_identity,
                post_response_compaction_checks_collector=post_response_compaction_checks,
                refresh_scheduler=refresh_scheduler,
            ),
        ),
    )

    # Peek at first event to detect errors before committing to SSE
    first_event = await anext(aiter(stream), None)
    if first_event is None:
        await stream.aclose()
        return _error_response(500, "Agent returned empty response", error_type="server_error")

    first_error = _extract_agent_stream_failure(first_event)
    if first_error is not None:
        logger.warning(
            "Stream returned error",
            model=agent_name,
            session_id=session_id,
            error=first_error,
        )
        await stream.aclose()
        return _error_response(500, "Agent execution failed", error_type="server_error")

    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    created = int(time.time())
    stream_completed = False
    stream_failed = False

    async def event_generator() -> AsyncIterator[str]:
        nonlocal stream_completed, stream_failed
        tool_state = _ToolStreamState()
        saw_text_delta = False
        completed_body: str | None = None
        try:
            # 1. Initial role announcement
            yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'role': 'assistant'})}\n\n"

            # 2. Yield the peeked first event
            if isinstance(first_event, RunCompletedEvent):
                completed_body = str(first_event.content) if first_event.content is not None else None
            else:
                text = _extract_stream_text(first_event, tool_state)
                if text:
                    if isinstance(first_event, (RunContentEvent, str)):
                        saw_text_delta = True
                    yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': text})}\n\n"

            # 3. Stream remaining content
            # Error strings after the first event are sent as content chunks
            # since we can't switch to an error HTTP status mid-stream.
            async for event in stream:
                if isinstance(event, RunCompletedEvent):
                    completed_body = str(event.content) if event.content is not None else completed_body
                    continue
                failure_text = _extract_agent_stream_failure(event)
                if failure_text is not None:
                    stream_failed = True
                    logger.warning(
                        "Stream emitted terminal failure",
                        model=agent_name,
                        session_id=session_id,
                        error=failure_text,
                    )
                    yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': failure_text})}\n\n"
                    break
                text = _extract_stream_text(event, tool_state)
                if text:
                    if isinstance(event, (RunContentEvent, str)):
                        saw_text_delta = True
                    yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': text})}\n\n"

            if completed_body and not saw_text_delta and not stream_failed:
                yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': completed_body})}\n\n"

            # 4. Final chunk with finish_reason
            logger.info("Chat completion sent", model=agent_name, stream=True)
            yield f"data: {_chunk_json(completion_id, created, agent_name, delta={}, finish_reason='stop')}\n\n"

            # 5. Stream terminator
            yield "data: [DONE]\n\n"
            stream_completed = not stream_failed
        finally:
            await stream.aclose()

    response = _OpenAIStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        background=_openai_post_response_compaction_task(
            post_response_compaction_checks,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        ),
    )
    response.completion_predicate = lambda: stream_completed
    return response


async def _run_openai_post_response_compaction_checks(
    checks: Sequence[PostResponseCompactionCheck],
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    """Run post-response compaction for OpenAI-compatible sessions without Matrix notices."""
    for check in checks:
        try:
            await run_post_response_compaction_check(
                check=check,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                compaction_lifecycle=None,
            )
        except Exception:
            logger.exception(
                "OpenAI-compatible post-response compaction failed",
                session_id=check.session_id,
                scope=check.scope.key,
            )


def _openai_post_response_compaction_task(
    checks: Sequence[PostResponseCompactionCheck],
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> BackgroundTask | None:
    """Return a response-finalizer task for post-response OpenAI compaction."""
    if not checks:
        return None
    return BackgroundTask(
        _run_openai_post_response_compaction_checks,
        tuple(checks),
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    )


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

    Raises ValueError when the configured team cannot be materialized.
    """
    team_config = config.teams[team_name]
    mode = TeamMode(team_config.mode)
    config_ids = config.get_ids(runtime_paths)
    requested_members = [config_ids[member_name] for member_name in team_config.agents]
    model_name = team_config.model or "default"

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
        final_resolution = resolve_configured_team(
            team_name,
            requested_members,
            mode,
            config,
            runtime_paths,
            materializable_agent_names=team_members.materialized_agent_names,
        )
        if final_resolution.outcome is not TeamOutcome.TEAM:
            _raise_unmaterializable_team(team_name, final_resolution.reason)

        team = build_materialized_team_instance(
            requested_agent_names=team_members.requested_agent_names,
            agents=team_members.agents,
            mode=mode,
            config=config,
            runtime_paths=runtime_paths,
            model_name=model_name,
            configured_team_name=team_name,
            scope_context=scope_context,
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


def _is_queued_notice_message(message: Message) -> bool:
    provider_data = message.provider_data
    return isinstance(provider_data, dict) and provider_data.get("mindroom_queued_message_notice") is True


def _scrub_queued_notice_team_scope_context(scope_context: ScopeSessionContext | None) -> None:
    """Strip stale queued-message notices from a loaded team session before replay."""
    if scope_context is None or scope_context.session is None:
        return
    changed = False
    for run in scope_context.session.runs or []:
        if not isinstance(run, (RunOutput, TeamRunOutput)) or not run.messages:
            continue
        filtered_messages = [message for message in run.messages if not _is_queued_notice_message(message)]
        if len(filtered_messages) == len(run.messages):
            continue
        run.messages = filtered_messages
        changed = True
    if changed:
        scope_context.storage.upsert_session(scope_context.session)


async def prepare_materialized_team_execution(
    *,
    scope_context: ScopeSessionContext | None,
    agents: list[Agent],
    team: Team,
    message: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    config: Config,
    runtime_paths: RuntimePaths,
    active_model_name: str | None,
    reply_to_event_id: str | None,
    active_event_ids: frozenset[str],
    response_sender_id: str | None,
    current_sender_id: str | None,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    correlation_id: str | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    configured_team_name: str | None,
    matrix_run_metadata: dict[str, object] | None = None,
    system_enrichment_items: tuple[object, ...] = (),
) -> _PreparedOpenAIMaterializedTeamExecution:
    """Prepare one team run using only public execution-preparation interfaces."""
    del system_enrichment_items
    _scrub_queued_notice_team_scope_context(scope_context)
    prepared_execution = await prepare_bound_team_run_context(
        scope_context=scope_context,
        agents=agents,
        team=team,
        prompt=message,
        thread_history=thread_history,
        config=config,
        runtime_paths=runtime_paths,
        entity_name=configured_team_name,
        active_model_name=active_model_name,
        active_context_window=config.resolve_runtime_model(
            entity_name=configured_team_name,
            active_model_name=active_model_name,
        ).context_window,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        compaction_outcomes_collector=compaction_outcomes_collector,
    )
    run_metadata = build_matrix_run_metadata(
        reply_to_event_id,
        prepared_execution.unseen_event_ids,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        extra_metadata=matrix_run_metadata,
    )
    return _PreparedOpenAIMaterializedTeamExecution(
        messages=prepared_execution.messages,
        run_metadata=cast("dict[str, object] | None", run_metadata),
        unseen_event_ids=prepared_execution.unseen_event_ids,
        post_response_compaction_checks=prepared_execution.post_response_compaction_checks,
    )


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
    post_response_compaction_checks_collector: list[PostResponseCompactionCheck] | None = None,
    user: str | None = None,
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
        requester_id=user or (execution_identity.requester_id if execution_identity is not None else None),
        correlation_id=uuid4().hex,
        compaction_outcomes_collector=None,
        configured_team_name=team_name,
        matrix_run_metadata=None,
        system_enrichment_items=(),
    )
    if post_response_compaction_checks_collector is not None:
        post_response_compaction_checks_collector.extend(prepared_execution.post_response_compaction_checks or [])
    return _PreparedOpenAITeamPrompt(
        prompt=render_prepared_team_messages_text(prepared_execution.messages),
        run_metadata=prepared_execution.run_metadata,
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
    post_response_compaction_checks: list[PostResponseCompactionCheck] = []
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
                agents, team, mode = _build_team(
                    team_name,
                    config,
                    runtime_paths,
                    execution_identity,
                    scope_context,
                    session_id,
                    unavailable_bases,
                    refresh_scheduler,
                )
            except ValueError as e:
                return _error_response(500, str(e), error_type="server_error")

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
                prepared_team_prompt = await _prepare_openai_team_prompt(
                    scope_context=scope_context,
                    team_name=team_name,
                    agents=agents,
                    team=team,
                    prompt=prompt,
                    config=config,
                    runtime_paths=runtime_paths,
                    thread_history=thread_history,
                    post_response_compaction_checks_collector=post_response_compaction_checks,
                    user=user,
                    execution_identity=execution_identity,
                )
            except Exception:
                logger.exception("Team member preparation failed", team=team_name)
                return _error_response(500, "Team execution failed", error_type="server_error")
            try:
                response = await team.arun(
                    prepared_team_prompt.prompt,
                    session_id=session_id,
                    user_id=user,
                    metadata=prepared_team_prompt.run_metadata,
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
            completion_id = f"chatcmpl-{uuid4().hex[:12]}"
            result = _ChatCompletionResponse(
                id=completion_id,
                created=int(time.time()),
                model=model_id,
                choices=[
                    _ChatCompletionChoice(
                        message=_ChatMessage(role="assistant", content=response_text),
                    ),
                ],
            )
            return _OpenAIJSONResponse(
                content=result.model_dump(),
                background=_openai_post_response_compaction_task(
                    post_response_compaction_checks,
                    runtime_paths=runtime_paths,
                    config=config,
                    execution_identity=execution_identity,
                ),
            )
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
    post_response_compaction_checks: list[PostResponseCompactionCheck] = []

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
                agents, team, mode = _build_team(
                    team_name,
                    config,
                    runtime_paths,
                    execution_identity,
                    scope_context,
                    session_id,
                    unavailable_bases,
                    refresh_scheduler,
                )
        except ValueError as e:
            await _cleanup()
            return _error_response(500, str(e), error_type="server_error")

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
            prepared_team_prompt = await _prepare_openai_team_prompt(
                scope_context=scope_context,
                team_name=team_name,
                agents=agents,
                team=team,
                prompt=prompt,
                config=config,
                runtime_paths=runtime_paths,
                thread_history=thread_history,
                post_response_compaction_checks_collector=post_response_compaction_checks,
                user=user,
                execution_identity=execution_identity,
            )
        except Exception:
            logger.exception("Team member preparation failed", team=team_name)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")
        try:
            stream = cast(
                "AsyncGenerator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput, None]",
                stream_with_tool_execution_identity(
                    execution_identity,
                    stream_factory=lambda: cast(
                        "AsyncGenerator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput, None]",
                        team.arun(
                            prepared_team_prompt.prompt,
                            stream=True,
                            stream_events=True,
                            session_id=session_id,
                            user_id=user,
                            metadata=prepared_team_prompt.run_metadata,
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
        first_error = _extract_team_stream_failure(first_event)
        if first_error is not None:
            logger.warning("Team streaming returned terminal failure", team=team_name, error=first_error)
            await _cleanup()
            return _error_response(500, "Team execution failed", error_type="server_error")

        completion_id = f"chatcmpl-{uuid4().hex[:12]}"
        created = int(time.time())
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
                    completion_id=completion_id,
                    created=created,
                    model_id=model_id,
                    team_name=team_name,
                    mark_stream_failed=mark_stream_failed,
                ):
                    yield chunk
                    if chunk == "data: [DONE]\n\n" and not stream_failed:
                        stream_completed = True
            finally:
                await _cleanup()

        response = _OpenAIStreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            background=_openai_post_response_compaction_task(
                post_response_compaction_checks,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
            ),
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
    tool_state: _ToolStreamState,
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
    tool_text = _format_stream_tool_event(event, tool_state)
    if tool_text is not None:
        return tool_text

    # Team leader content — stream directly (synthesized answer)
    if isinstance(event, TeamContentEvent) and event.content:
        return str(event.content)

    # Everything else (member content, reasoning, memory, hooks, etc.) — skip.
    return None


def _finalize_pending_tools(tool_state: _ToolStreamState) -> str | None:
    """Build done tags for tool calls that started but never completed."""
    if not tool_state.tool_ids_by_call_id:
        return None
    parts = [
        f'<tool id="{tool_id}" state="done">(interrupted)</tool>' for tool_id in tool_state.tool_ids_by_call_id.values()
    ]
    tool_state.tool_ids_by_call_id.clear()
    return "".join(parts)


async def _team_stream_event_generator(
    *,
    stream: AsyncIterator[RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput],
    first_event: RunOutputEvent | TeamRunOutputEvent | RunOutput | TeamRunOutput,
    completion_id: str,
    created: int,
    model_id: str,
    team_name: str,
    mark_stream_failed: Callable[[], None],
) -> AsyncIterator[str]:
    """Yield SSE chunks for team streaming responses.

    Streams team leader content (``TeamContentEvent``) directly for real-time output.
    Skips member agent content (``RunContentEvent``) to prevent interleaving.
    Emits all tool events (agent-level and team-level) for progress feedback.

    The caller (``_stream_team_completion``) validates the first event via
    ``_team_stream_preflight_error`` before entering this generator, so
    ``first_event`` is guaranteed to be non-error.
    """
    tool_state = _ToolStreamState()

    def _chunk(content: str) -> str:
        return f"data: {_chunk_json(completion_id, created, model_id, delta={'content': content})}\n\n"

    # 1. Role announcement
    yield f"data: {_chunk_json(completion_id, created, model_id, delta={'role': 'assistant'})}\n\n"

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
                pending = _finalize_pending_tools(tool_state)
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
        pending = _finalize_pending_tools(tool_state)
        if pending:
            yield _chunk(pending)
        yield _chunk("Team execution failed.")

    # 4. Finalize any tool calls that started but never completed
    pending = _finalize_pending_tools(tool_state)
    if pending:
        yield _chunk(pending)

    # 5. Finish
    logger.info("Team completion sent", team=team_name, stream=True)
    yield f"data: {_chunk_json(completion_id, created, model_id, delta={}, finish_reason='stop')}\n\n"
    yield "data: [DONE]\n\n"
