"""OpenAI-compatible chat completions API for MindRoom agents.

Exposes MindRoom agents as an OpenAI-compatible API so any chat frontend
(LibreChat, Open WebUI, LobeChat, etc.) can use them as selectable "models".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple
from uuid import uuid4

from agno.run.agent import RunContentEvent, RunErrorEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.team import RunContentEvent as TeamContentEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutput
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from agno.team import Team
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.agents import create_agent
from mindroom.ai import (
    AIStreamChunk,
    ai_response,
    build_prompt_with_thread_history,
    get_model_instance,
    stream_agent_response,
)
from mindroom.config import Config
from mindroom.constants import DEFAULT_AGENTS_CONFIG, ROUTER_AGENT_NAME, STORAGE_PATH_OBJ
from mindroom.knowledge import get_knowledge_manager, initialize_knowledge_managers
from mindroom.knowledge_utils import resolve_agent_knowledge
from mindroom.logging_config import get_logger
from mindroom.routing import suggest_agent
from mindroom.teams import TeamMode, format_team_response
from mindroom.tool_events import extract_tool_completed_info, format_tool_started_event

AUTO_MODEL_NAME = "auto"
TEAM_MODEL_PREFIX = "team/"
RESERVED_MODEL_NAMES = {AUTO_MODEL_NAME}

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.run.agent import RunOutput, RunOutputEvent
    from agno.run.team import TeamRunOutputEvent

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])


def _load_config() -> tuple[Config, Path]:
    """Load the current runtime config and return it with its path.

    Loads directly from Config.from_yaml rather than sharing with main.py's
    loader to avoid circular imports (main.py imports this router).
    """
    return Config.from_yaml(DEFAULT_AGENTS_CONFIG), DEFAULT_AGENTS_CONFIG


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single message in the chat conversation."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[dict] | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage]
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


class ChatCompletionChoice(BaseModel):
    """A single choice in a chat completion response."""

    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class UsageInfo(BaseModel):
    """Token usage information (always zeros — Agno doesn't expose counts)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """Non-streaming chat completion response."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)
    system_fingerprint: str | None = None


# --- Streaming response models ---


class ChatCompletionChunkChoice(BaseModel):
    """A single choice in a streaming chunk."""

    index: int = 0
    delta: dict
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """A single SSE chunk in a streaming response."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    system_fingerprint: str | None = None


# --- Model listing ---


class ModelObject(BaseModel):
    """A model (agent) entry for the /v1/models response."""

    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "mindroom"
    name: str | None = None
    description: str | None = None


class ModelListResponse(BaseModel):
    """Response for GET /v1/models."""

    object: str = "list"
    data: list[ModelObject]


# --- Error response ---


class OpenAIError(BaseModel):
    """OpenAI-compatible error detail."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class OpenAIErrorResponse(BaseModel):
    """OpenAI-compatible error wrapper."""

    error: OpenAIError


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
    body = OpenAIErrorResponse(
        error=OpenAIError(message=message, type=error_type, param=param, code=code),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _verify_api_key(authorization: str | None) -> JSONResponse | None:
    """Verify bearer token against OPENAI_COMPAT_API_KEYS.

    Returns None if valid, or an error JSONResponse if invalid.
    """
    keys_env = os.getenv("OPENAI_COMPAT_API_KEYS", "")
    allow_unauthenticated = os.getenv("OPENAI_COMPAT_ALLOW_UNAUTHENTICATED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
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
    conversation: list[dict[str, str]],
) -> tuple[str, list[dict[str, str]] | None] | None:
    """Find the last user message and split into (prompt, thread_history).

    Returns None if no user message exists.
    """
    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i]["sender"] == "user":
            prompt = conversation[i]["body"]
            history = conversation[:i] if i > 0 else None
            return prompt, history
    return None


def _convert_messages(
    messages: list[ChatMessage],
) -> tuple[str, list[dict[str, Any]] | None]:
    """Convert OpenAI messages to MindRoom's (prompt, thread_history) format.

    Returns:
        Tuple of (prompt, thread_history).

    """
    system_parts: list[str] = []
    conversation: list[dict[str, str]] = []

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
                conversation.append({"sender": msg.role, "body": text})

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
    req: ChatCompletionRequest,
    config: Config,
) -> JSONResponse | None:
    """Validate a chat completion request. Returns error response or None if valid."""
    if not req.messages:
        return _error_response(400, "Messages array is required and must not be empty")

    agent_name = req.model

    if agent_name.startswith(TEAM_MODEL_PREFIX):
        team_name = agent_name.removeprefix(TEAM_MODEL_PREFIX)
        if not config.teams or team_name not in config.teams:
            return _error_response(
                404,
                f"Team '{team_name}' not found",
                param="model",
                code="model_not_found",
            )
        return None  # team execution handled in chat_completions

    if agent_name == AUTO_MODEL_NAME:
        return None  # auto-routing handled in chat_completions

    if agent_name not in config.agents or agent_name == ROUTER_AGENT_NAME or agent_name in RESERVED_MODEL_NAMES:
        return _error_response(
            404,
            f"Model '{agent_name}' not found",
            param="model",
            code="model_not_found",
        )

    return None


def _parse_chat_request(
    body: bytes,
) -> tuple[ChatCompletionRequest, Config, str, list[dict[str, Any]] | None] | JSONResponse:
    """Parse and validate a chat completion request body.

    Returns (request, config, prompt, thread_history) on success, or a JSONResponse error.
    """
    try:
        req = ChatCompletionRequest(**json.loads(body))
    except (json.JSONDecodeError, ValidationError):
        return _error_response(400, "Invalid request body")

    config, _ = _load_config()
    validation_error = _validate_chat_request(req, config)
    if validation_error:
        return validation_error

    prompt, thread_history = _convert_messages(req.messages)
    if not prompt:
        return _error_response(400, "No user message content found in messages")

    return req, config, prompt, thread_history


async def _resolve_auto_route(
    prompt: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
) -> str | JSONResponse:
    """Resolve auto-routing to a specific agent name.

    Returns the resolved agent name, or a JSONResponse error if routing fails
    and no agents are available.
    """
    available = [n for n in config.agents if n != ROUTER_AGENT_NAME]
    routed = await suggest_agent(prompt, available, config, thread_history)
    if routed is None:
        if not available:
            return _error_response(500, "No agents configured for auto-routing", error_type="server_error")
        routed = available[0]
        logger.warning("Auto-routing failed, falling back", agent=routed)
    else:
        logger.info("Auto-routed", requested="auto", resolved=routed)
    return routed


async def _ensure_knowledge_initialized(config: Config) -> None:
    """Initialize knowledge managers if needed.

    Safe to call multiple times — `initialize_knowledge_managers` is
    idempotent and reuses existing managers that match the config.
    """
    if not config.knowledge_bases:
        return
    await initialize_knowledge_managers(
        config=config,
        storage_path=STORAGE_PATH_OBJ,
        start_watchers=False,
        reindex_on_create=False,
    )


def _resolve_knowledge(agent_name: str, config: Config) -> Knowledge | None:
    """Resolve knowledge base(s) for an agent from the global knowledge managers.

    Mirrors the logic in bot.py's AgentBot._knowledge_for_agent().
    """
    return resolve_agent_knowledge(
        agent_name,
        config,
        lambda base_id: (manager.get_knowledge() if (manager := get_knowledge_manager(base_id)) is not None else None),
        on_missing_bases=lambda missing_base_ids: logger.warning(
            "Knowledge bases not available for agent",
            agent=agent_name,
            knowledge_bases=missing_base_ids,
        ),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models(
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """List available models (agents) in OpenAI format."""
    auth_error = _verify_api_key(authorization)
    if auth_error:
        return auth_error

    config, config_path = _load_config()

    # Use config file mtime as creation timestamp
    try:
        created = int(config_path.stat().st_mtime)
    except OSError:
        created = 0

    models: list[ModelObject] = [
        ModelObject(
            id=AUTO_MODEL_NAME,
            name="Auto",
            description="Automatically routes to the best agent for your message",
            created=created,
        ),
    ]
    for agent_name, agent_config in config.agents.items():
        if agent_name == ROUTER_AGENT_NAME or agent_name in RESERVED_MODEL_NAMES:
            continue
        models.append(
            ModelObject(
                id=agent_name,
                name=agent_config.display_name,
                description=agent_config.role or None,
                created=created,
            ),
        )

    # Add teams
    for team_name, team_config in (config.teams or {}).items():
        models.append(
            ModelObject(
                id=f"{TEAM_MODEL_PREFIX}{team_name}",
                name=team_config.display_name,
                description=team_config.role or None,
                created=created,
            ),
        )

    response = ModelListResponse(data=models)
    return JSONResponse(content=response.model_dump())


@router.post("/chat/completions", response_model=None)
async def chat_completions(  # noqa: PLR0911
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse | StreamingResponse:
    """Create a chat completion (non-streaming or streaming)."""
    auth_error = _verify_api_key(authorization)
    if auth_error:
        return auth_error

    # Parse and validate request
    parsed = _parse_chat_request(await request.body())
    if isinstance(parsed, JSONResponse):
        return parsed
    req, config, prompt, thread_history = parsed

    # Resolve auto-routing if model is "auto"
    agent_name = req.model
    if agent_name == AUTO_MODEL_NAME:
        result = await _resolve_auto_route(prompt, config, thread_history)
        if isinstance(result, JSONResponse):
            return result
        agent_name = result

    # Derive a namespaced session ID from request headers or fallback UUID.
    session_id = _derive_session_id(agent_name, request)
    tool_event_format = request.headers.get("x-tool-event-format")
    logger.info(
        "Chat completion request",
        model=agent_name,
        stream=req.stream,
        session_id=session_id,
    )

    # Initialize shared knowledge managers once per request (idempotent).
    # Team and single-agent paths both rely on this for knowledge resolution.
    try:
        await _ensure_knowledge_initialized(config)
    except Exception:
        logger.warning("Knowledge initialization failed, proceeding without knowledge", exc_info=True)

    # Team execution path
    if agent_name.startswith(TEAM_MODEL_PREFIX):
        team_name = agent_name.removeprefix(TEAM_MODEL_PREFIX)
        if req.stream:
            return await _stream_team_completion(
                team_name,
                agent_name,
                prompt,
                session_id,
                config,
                thread_history,
                req.user,
                tool_event_format=tool_event_format,
            )
        return await _non_stream_team_completion(
            team_name,
            agent_name,
            prompt,
            session_id,
            config,
            thread_history,
            req.user,
        )

    # Resolve knowledge base for this agent
    try:
        knowledge = _resolve_knowledge(agent_name, config)
    except Exception:
        logger.warning("Knowledge resolution failed, proceeding without knowledge", exc_info=True)
        knowledge = None

    if req.stream:
        return await _stream_completion(
            agent_name,
            prompt,
            session_id,
            config,
            thread_history,
            req.user,
            knowledge,
            tool_event_format=tool_event_format,
        )
    return await _non_stream_completion(agent_name, prompt, session_id, config, thread_history, req.user, knowledge)


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


async def _non_stream_completion(
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
    user: str | None,
    knowledge: Knowledge | None = None,
) -> JSONResponse:
    """Handle non-streaming chat completion."""
    response_text = await ai_response(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        storage_path=STORAGE_PATH_OBJ,
        config=config,
        thread_history=thread_history,
        room_id=None,
        knowledge=knowledge,
        user_id=user,
        include_default_tools=False,
    )

    # Detect error responses from ai_response()
    if _is_error_response(response_text):
        logger.warning("AI response returned error", model=agent_name, session_id=session_id, error=response_text)
        return _error_response(500, "Agent execution failed", error_type="server_error")

    logger.info("Chat completion sent", model=agent_name, stream=False, session_id=session_id)
    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    response = ChatCompletionResponse(
        id=completion_id,
        created=int(time.time()),
        model=agent_name,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=response_text),
            ),
        ],
    )
    return JSONResponse(content=response.model_dump())


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
    chunk = ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason),
        ],
    )
    return chunk.model_dump_json()


def _format_stream_tool_event(event: RunOutputEvent | TeamRunOutputEvent) -> str | None:
    """Format a tool event as inline text for the SSE stream."""
    if isinstance(event, (ToolCallStartedEvent, TeamToolCallStartedEvent)):
        tool_msg, _ = format_tool_started_event(event.tool)
        return tool_msg or None
    if isinstance(event, (ToolCallCompletedEvent, TeamToolCallCompletedEvent)):
        info = extract_tool_completed_info(event.tool)
        if info:
            _tool_name, result = info
            return f"\nResult: {result}\n" if result else None
    return None


# ---------------------------------------------------------------------------
# LibreChat structured tool events
# ---------------------------------------------------------------------------


def _librechat_step_event(event_name: str, data: dict[str, Any]) -> str:
    r"""Format a LibreChat structured SSE step event.

    LibreChat expects: data: {"event": "<name>", "data": {...}}\n\n
    The event field is inside the JSON payload, not an SSE named event.
    """
    return f"data: {json.dumps({'event': event_name, 'data': data})}\n\n"


def _librechat_tool_start_sse(
    step_id: str,
    call_id: str,
    run_id: str,
    step_index: int,
    tool_name: str,
    args_json: str,
) -> str:
    """Format an on_run_step SSE line for a tool call start."""
    return _librechat_step_event(
        "on_run_step",
        {
            "id": step_id,
            "runId": run_id,
            "index": step_index,
            "type": "tool_calls",
            "stepDetails": {
                "type": "tool_calls",
                "tool_calls": [
                    {
                        "id": call_id,
                        "name": tool_name,
                        "args": args_json,
                        "type": "tool_call",
                    },
                ],
            },
            "usage": None,
        },
    )


def _librechat_tool_end_sse(
    step_id: str,
    call_id: str,
    step_index: int,
    tool_name: str,
    args_json: str,
    output: str,
) -> str:
    """Format an on_run_step_completed SSE line for a tool call completion."""
    return _librechat_step_event(
        "on_run_step_completed",
        {
            "result": {
                "id": step_id,
                "index": step_index,
                "tool_call": {
                    "id": call_id,
                    "name": tool_name,
                    "args": args_json,
                    "output": output,
                    "progress": 1,
                    "type": "tool_call",
                },
            },
        },
    )


class _PendingToolCall(NamedTuple):
    """State stored for a tool call between start and complete events."""

    step_id: str
    call_id: str
    step_index: int
    tool_name: str
    args_json: str


class _LibreChatToolTracker:
    """Track tool call state for LibreChat structured SSE events.

    Converts agno ToolCallStartedEvent/ToolCallCompletedEvent pairs into
    LibreChat's on_run_step/on_run_step_completed SSE events with matched IDs.

    Tool calls are correlated by ``tool_call_id`` from agno's ToolExecution
    so concurrent/parallel tool calls are completed correctly (not LIFO).
    """

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._step_index = 0
        self._pending: dict[str, _PendingToolCall] = {}

    def handle_event(self, event: object) -> str | None:
        """Try to handle event as a tool event. Returns SSE line or None."""
        if isinstance(event, (ToolCallStartedEvent, TeamToolCallStartedEvent)):
            return self._on_start(event.tool)
        if isinstance(event, (ToolCallCompletedEvent, TeamToolCallCompletedEvent)):
            return self._on_complete(event.tool)
        return None

    def _on_start(self, tool: object) -> str | None:
        if tool is None:
            return None
        tool_name = getattr(tool, "tool_name", None) or "tool"
        raw_args = getattr(tool, "tool_args", None)
        tool_args = {str(k): v for k, v in raw_args.items()} if isinstance(raw_args, dict) else {}
        args_json = json.dumps(tool_args, default=str)

        idx = self._step_index
        self._step_index += 1
        step_id = f"step-{uuid4().hex[:8]}"
        call_id = f"call-{uuid4().hex[:8]}"

        tool_call_id = getattr(tool, "tool_call_id", None) or call_id
        self._pending[tool_call_id] = _PendingToolCall(step_id, call_id, idx, tool_name, args_json)
        return _librechat_tool_start_sse(step_id, call_id, self._run_id, idx, tool_name, args_json)

    def _on_complete(self, tool: object) -> str | None:
        if tool is None:
            return None
        result = str(getattr(tool, "result", "") or "")
        tool_call_id = getattr(tool, "tool_call_id", None)

        pending = self._pending.pop(tool_call_id, None) if tool_call_id else None
        if pending is None:
            return None
        return _librechat_tool_end_sse(
            pending.step_id,
            pending.call_id,
            pending.step_index,
            pending.tool_name,
            pending.args_json,
            result,
        )


def _extract_stream_text(event: AIStreamChunk) -> str | None:
    """Extract text content from a stream event."""
    if isinstance(event, RunContentEvent) and event.content:
        return str(event.content)
    if isinstance(event, str):
        return event
    return _format_stream_tool_event(event)


async def _stream_completion(
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
    user: str | None,
    knowledge: Knowledge | None = None,
    *,
    tool_event_format: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming chat completion via SSE."""
    stream: AsyncIterator[AIStreamChunk] = stream_agent_response(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        storage_path=STORAGE_PATH_OBJ,
        config=config,
        thread_history=thread_history,
        room_id=None,
        knowledge=knowledge,
        user_id=user,
        include_default_tools=False,
    )

    # Peek at first event to detect errors before committing to SSE
    first_event = await anext(aiter(stream), None)
    if first_event is None:
        return _error_response(500, "Agent returned empty response", error_type="server_error")

    if isinstance(first_event, str) and _is_error_response(first_event):
        logger.warning("Stream returned error", model=agent_name, session_id=session_id, error=first_event)
        return _error_response(500, "Agent execution failed", error_type="server_error")

    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    created = int(time.time())
    tracker = _LibreChatToolTracker(completion_id) if tool_event_format == "librechat" else None

    async def event_generator() -> AsyncIterator[str]:
        # 1. Initial role announcement
        yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'role': 'assistant'})}\n\n"

        # 2. Yield the peeked first event
        lc_sse = tracker.handle_event(first_event) if tracker else None
        if lc_sse:
            yield lc_sse
        else:
            text = _extract_stream_text(first_event)
            if text:
                yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': text})}\n\n"

        # 3. Stream remaining content
        # Error strings after the first event are sent as content chunks
        # since we can't switch to an error HTTP status mid-stream.
        async for event in stream:
            lc_sse = tracker.handle_event(event) if tracker else None
            if lc_sse:
                yield lc_sse
                continue
            text = _extract_stream_text(event)
            if text:
                yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': text})}\n\n"

        # 4. Final chunk with finish_reason
        logger.info("Chat completion sent", model=agent_name, stream=True)
        yield f"data: {_chunk_json(completion_id, created, agent_name, delta={}, finish_reason='stop')}\n\n"

        # 5. Stream terminator
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Team completion
# ---------------------------------------------------------------------------


def _build_team(team_name: str, config: Config) -> tuple[list[Agent], Team | None, TeamMode]:
    """Create agents and build an agno.Team for the given team config.

    Returns (agents, team, mode). When no agents can be created,
    returns ([], None, mode) so callers can handle it gracefully.
    """
    team_config = config.teams[team_name]
    mode = TeamMode(team_config.mode)
    model_name = team_config.model or "default"
    model = get_model_instance(config, model_name)

    agents: list[Agent] = []
    for member_name in team_config.agents:
        if member_name not in config.agents or member_name == ROUTER_AGENT_NAME:
            logger.warning("Team member not found, skipping", team=team_name, agent=member_name)
            continue
        try:
            agents.append(
                create_agent(
                    member_name,
                    config,
                    storage_path=STORAGE_PATH_OBJ,
                    knowledge=_resolve_knowledge(member_name, config),
                    include_default_tools=False,
                ),
            )
        except Exception:
            logger.warning("Failed to create team member, skipping", team=team_name, agent=member_name, exc_info=True)

    if not agents:
        return [], None, mode

    team = Team(
        members=agents,  # type: ignore[arg-type]
        name=f"Team-{team_name}",
        model=model,
        delegate_to_all_members=mode == TeamMode.COLLABORATE,
        show_members_responses=True,
        debug_mode=False,
    )
    return agents, team, mode


def _format_team_output(response: TeamRunOutput | RunOutput) -> str:
    """Format a TeamRunOutput into a single string for the API response."""
    parts = format_team_response(response)
    return "\n\n".join(parts) if parts else str(response.content or "")


async def _non_stream_team_completion(
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
    user: str | None = None,
) -> JSONResponse:
    """Handle non-streaming team completion."""
    agents, team, mode = _build_team(team_name, config)
    if not agents or team is None:
        return _error_response(500, "No valid agents found for team", error_type="server_error")

    logger.info("Team completion request", team=team_name, mode=mode.value, members=len(agents), session_id=session_id)

    team_prompt = build_prompt_with_thread_history(prompt, thread_history)

    try:
        response = await team.arun(team_prompt, session_id=session_id, user_id=user)
    except Exception:
        logger.exception("Team execution failed", team=team_name)
        return _error_response(500, "Team execution failed", error_type="server_error")

    response_text = _format_team_output(response) if isinstance(response, TeamRunOutput) else str(response)

    if _is_error_response(response_text):
        logger.warning("Team response returned error", team=team_name, error=response_text)
        return _error_response(500, "Team execution failed", error_type="server_error")

    logger.info("Team completion sent", team=team_name, stream=False)
    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    result = ChatCompletionResponse(
        id=completion_id,
        created=int(time.time()),
        model=model_id,
        choices=[
            ChatCompletionChoice(
                message=ChatMessage(role="assistant", content=response_text),
            ),
        ],
    )
    return JSONResponse(content=result.model_dump())


async def _stream_team_completion(
    team_name: str,
    model_id: str,
    prompt: str,
    session_id: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
    user: str | None = None,
    *,
    tool_event_format: str | None = None,
) -> StreamingResponse | JSONResponse:
    """Handle streaming team completion via SSE."""
    agents, team, mode = _build_team(team_name, config)
    if not agents or team is None:
        return _error_response(500, "No valid agents found for team", error_type="server_error")

    logger.info("Team streaming request", team=team_name, mode=mode.value, members=len(agents), session_id=session_id)

    team_prompt = build_prompt_with_thread_history(prompt, thread_history)

    try:
        stream = team.arun(team_prompt, stream=True, stream_events=True, session_id=session_id, user_id=user)
        # Peek at first event
        first_event = await anext(aiter(stream), None)
    except Exception:
        logger.exception("Team execution failed", team=team_name)
        return _error_response(500, "Team execution failed", error_type="server_error")

    preflight_error = _team_stream_preflight_error(first_event, team_name)
    if preflight_error is not None:
        return preflight_error

    assert first_event is not None

    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    created = int(time.time())
    return StreamingResponse(
        _team_stream_event_generator(
            stream=stream,
            first_event=first_event,
            completion_id=completion_id,
            created=created,
            model_id=model_id,
            team_name=team_name,
            tool_event_format=tool_event_format,
        ),
        media_type="text/event-stream",
    )


def _extract_team_stream_text(event: RunOutputEvent | TeamRunOutputEvent | TeamRunOutput | str) -> str | None:
    """Extract text content from a team stream event."""
    if isinstance(event, TeamContentEvent) and event.content:
        return str(event.content)
    if isinstance(event, RunContentEvent) and event.content:
        return str(event.content)
    if isinstance(event, TeamRunOutput):
        return _format_team_output(event)
    if isinstance(event, str):
        return event
    return _format_stream_tool_event(event)


def _extract_team_stream_error(event: RunOutputEvent | TeamRunOutputEvent | TeamRunOutput | str) -> str | None:
    """Extract explicit error text from a team stream event."""
    if isinstance(event, (RunErrorEvent, TeamRunErrorEvent)):
        return str(event.content or "Unknown team error")
    if isinstance(event, str) and _is_error_response(event):
        return event
    return None


def _team_stream_preflight_error(
    first_event: RunOutputEvent | TeamRunOutputEvent | TeamRunOutput | str | None,
    team_name: str,
) -> JSONResponse | None:
    """Validate first team stream event before SSE response begins."""
    if first_event is None:
        return _error_response(500, "Team returned empty response", error_type="server_error")

    first_error = _extract_team_stream_error(first_event)
    if first_error is None:
        return None

    logger.warning("Team streaming returned error", team=team_name, error=first_error)
    return _error_response(500, "Team execution failed", error_type="server_error")


async def _team_stream_event_generator(
    *,
    stream: AsyncIterator[RunOutputEvent | TeamRunOutputEvent | TeamRunOutput | str],
    first_event: RunOutputEvent | TeamRunOutputEvent | TeamRunOutput | str,
    completion_id: str,
    created: int,
    model_id: str,
    team_name: str,
    tool_event_format: str | None = None,
) -> AsyncIterator[str]:
    """Yield SSE chunks for team streaming responses."""
    tracker = _LibreChatToolTracker(completion_id) if tool_event_format == "librechat" else None

    # 1. Role announcement
    yield f"data: {_chunk_json(completion_id, created, model_id, delta={'role': 'assistant'})}\n\n"

    # 2. First event
    lc_sse = tracker.handle_event(first_event) if tracker else None
    if lc_sse:
        yield lc_sse
    else:
        first_text = _extract_team_stream_text(first_event)
        if first_text:
            yield f"data: {_chunk_json(completion_id, created, model_id, delta={'content': first_text})}\n\n"

    # 3. Remaining events
    try:
        async for event in stream:
            error_text = _extract_team_stream_error(event)
            if error_text is not None:
                logger.warning("Team stream emitted error event", team=team_name, error=error_text)
                yield f"data: {_chunk_json(completion_id, created, model_id, delta={'content': 'Team execution failed.'})}\n\n"
                break

            lc_sse = tracker.handle_event(event) if tracker else None
            if lc_sse:
                yield lc_sse
                continue

            text = _extract_team_stream_text(event)
            if text:
                yield f"data: {_chunk_json(completion_id, created, model_id, delta={'content': text})}\n\n"
    except Exception:
        logger.exception("Team stream failed during iteration", team=team_name)
        yield f"data: {_chunk_json(completion_id, created, model_id, delta={'content': 'Team execution failed.'})}\n\n"

    # 4. Finish
    logger.info("Team completion sent", team=team_name, stream=True)
    yield f"data: {_chunk_json(completion_id, created, model_id, delta={}, finish_reason='stop')}\n\n"
    yield "data: [DONE]\n\n"
