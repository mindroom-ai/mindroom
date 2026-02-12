"""OpenAI-compatible chat completions API for MindRoom agents.

Exposes MindRoom agents as an OpenAI-compatible API so any chat frontend
(LibreChat, Open WebUI, LobeChat, etc.) can use them as selectable "models".
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal
from uuid import uuid4

from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from mindroom.ai import AIStreamChunk, ai_response, stream_agent_response
from mindroom.config import Config
from mindroom.constants import DEFAULT_AGENTS_CONFIG, ROUTER_AGENT_NAME, STORAGE_PATH_OBJ
from mindroom.logging_config import get_logger
from mindroom.routing import suggest_agent
from mindroom.tool_events import extract_tool_completed_info, format_tool_started_event

AUTO_MODEL_NAME = "auto"

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

logger = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Compatible"])


def _load_config() -> tuple[Config, Path]:
    """Load the current runtime config and return it with its path."""
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
    if not keys_env.strip():
        # No keys configured — allow unauthenticated access
        return None

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

    These come from get_user_friendly_error_message() and start with emoji prefixes.
    """
    error_prefixes = ("❌", "⏱️", "⏰", "⚠️")
    stripped = text.lstrip()
    # Check for [agent_name] prefix followed by error emoji
    if stripped.startswith("["):
        bracket_end = stripped.find("]")
        if bracket_end != -1:
            after_bracket = stripped[bracket_end + 1 :].lstrip()
            return any(after_bracket.startswith(p) for p in error_prefixes)
    return any(stripped.startswith(p) for p in error_prefixes)


def _extract_content_text(content: str | list[dict] | None) -> str:
    """Extract text from a message content field.

    Handles string content and multimodal content lists.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Multimodal: concatenate text parts
    return " ".join(p["text"] for p in content if isinstance(p, dict) and p.get("type") == "text" and "text" in p)


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
            # Skip tool messages — agent handles its own tool calls
            continue
        elif msg.role in ("user", "assistant"):
            text = _extract_content_text(msg.content)
            if text:
                conversation.append({"sender": msg.role, "body": text})

    if not conversation:
        # No user/assistant messages — use system message as prompt
        prompt = "\n\n".join(system_parts) if system_parts else ""
        return prompt, None

    # Last user message becomes the prompt
    prompt = conversation[-1]["body"]
    thread_history = conversation[:-1] if len(conversation) > 1 else None

    # Prepend system message to prompt
    if system_parts:
        system_context = "\n\n".join(system_parts)
        prompt = f"{system_context}\n\n{prompt}"

    return prompt, thread_history


def _derive_session_id(
    model: str,
    user: str | None,
    first_user_message: str,
    request: Request,
) -> str:
    """Derive a session ID from request headers or content.

    Priority cascade:
    1. X-Session-Id header
    2. X-LibreChat-Conversation-Id header + model
    3. Hash of (model, user, first_user_message) — uses the first user message
       so the session ID stays stable across all messages in a conversation.
    """
    # 1. Explicit session ID
    session_id = request.headers.get("x-session-id")
    if session_id:
        return session_id

    # 2. LibreChat conversation ID
    libre_id = request.headers.get("x-librechat-conversation-id")
    if libre_id:
        return f"{libre_id}:{model}"

    # 3. Deterministic hash fallback
    user_id = user or "anonymous"
    hash_input = f"{model}:{user_id}:{first_user_message}"
    return hashlib.sha256(hash_input.encode()).hexdigest()[:16]


def _validate_chat_request(
    req: ChatCompletionRequest,
    config: Config,
) -> JSONResponse | None:
    """Validate a chat completion request. Returns error response or None if valid."""
    if not req.messages:
        return _error_response(400, "Messages array is required and must not be empty")

    agent_name = req.model

    if agent_name.startswith("team/"):
        return _error_response(
            501,
            "Team support via OpenAI API is not yet available",
            code="not_implemented",
        )

    if agent_name == AUTO_MODEL_NAME:
        return None  # auto-routing handled in chat_completions

    if agent_name not in config.agents or agent_name == ROUTER_AGENT_NAME:
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
    except Exception:
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
    logger.info("Auto-routed", requested="auto", resolved=routed)
    return routed


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
        if agent_name == ROUTER_AGENT_NAME:
            continue
        models.append(
            ModelObject(
                id=agent_name,
                name=agent_config.display_name,
                description=agent_config.role or None,
                created=created,
            ),
        )

    response = ModelListResponse(data=models)
    return JSONResponse(content=response.model_dump())


@router.post("/chat/completions", response_model=None)
async def chat_completions(
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

    # Derive session ID using first user message for stable hashing
    first_user_content = next(
        (_extract_content_text(m.content) for m in req.messages if m.role == "user"),
        prompt,
    )
    session_id = _derive_session_id(agent_name, req.user, first_user_content, request)
    logger.info(
        "Chat completion request",
        model=agent_name,
        stream=req.stream,
        session_id=session_id,
    )

    if req.stream:
        return await _stream_completion(agent_name, prompt, session_id, config, thread_history, req.user)
    return await _non_stream_completion(agent_name, prompt, session_id, config, thread_history, req.user)


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
        knowledge=None,
        user_id=user,
        include_default_tools=False,
    )

    # Detect error responses from ai_response()
    if _is_error_response(response_text):
        logger.warning("AI response returned error", model=agent_name, session_id=session_id)
        return _error_response(500, response_text, error_type="server_error")

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


def _format_stream_tool_event(event: object) -> str | None:
    """Format a tool event as inline text for the SSE stream."""
    if isinstance(event, ToolCallStartedEvent):
        tool_msg, _ = format_tool_started_event(event.tool)
        return tool_msg or None
    if isinstance(event, ToolCallCompletedEvent):
        info = extract_tool_completed_info(event.tool)
        if info:
            _tool_name, result = info
            return f"\nResult: {result}\n" if result else None
    return None


async def _stream_completion(
    agent_name: str,
    prompt: str,
    session_id: str,
    config: Config,
    thread_history: list[dict[str, Any]] | None,
    user: str | None,
) -> StreamingResponse:
    """Handle streaming chat completion via SSE."""
    completion_id = f"chatcmpl-{uuid4().hex[:12]}"
    created = int(time.time())

    async def event_generator() -> AsyncIterator[str]:
        # 1. Initial role announcement
        yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'role': 'assistant'})}\n\n"

        # 2. Stream content
        stream: AsyncIterator[AIStreamChunk] = stream_agent_response(
            agent_name=agent_name,
            prompt=prompt,
            session_id=session_id,
            storage_path=STORAGE_PATH_OBJ,
            config=config,
            thread_history=thread_history,
            room_id=None,
            knowledge=None,
            user_id=user,
            include_default_tools=False,
        )

        # Error strings from stream_agent_response() are sent as content chunks
        # since we can't switch to an error HTTP status mid-stream.
        async for event in stream:
            text: str | None
            if isinstance(event, RunContentEvent) and event.content:
                text = str(event.content)
            elif isinstance(event, str):
                text = event
            else:
                text = _format_stream_tool_event(event)

            if text:
                yield f"data: {_chunk_json(completion_id, created, agent_name, delta={'content': text})}\n\n"

        # 3. Final chunk with finish_reason
        logger.info("Chat completion sent", model=agent_name, stream=True)
        yield f"data: {_chunk_json(completion_id, created, agent_name, delta={}, finish_reason='stop')}\n\n"

        # 4. Stream terminator
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
