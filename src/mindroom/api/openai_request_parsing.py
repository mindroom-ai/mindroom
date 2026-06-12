"""Request parsing and model-identity resolution for the /v1 endpoint.

Pure given the request payload and the current config snapshot: validates
chat-completion bodies, converts OpenAI messages into MindRoom prompt and
thread-history inputs, derives session IDs, and resolves which agent or
team a requested model name maps to.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from mindroom.api.openai_streaming_protocol import error_response
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import WorkerScope

AUTO_MODEL_NAME = "auto"
TEAM_MODEL_PREFIX = "team/"
RESERVED_MODEL_NAMES = {AUTO_MODEL_NAME}

_OPENAI_COMPAT_SUPPORTED_WORKER_SCOPES: frozenset[WorkerScope | None] = frozenset({None, "shared"})


# ---------------------------------------------------------------------------
# Request models
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


def parse_chat_completion_body(body: bytes) -> ChatCompletionRequest | JSONResponse:
    """Parse one chat completion request body, or return an OpenAI-style error.

    Validates the decoded text directly so invalid JSON and valid-but-non-object
    JSON bodies (``null``, ``[]``, scalars) both surface as ValidationError
    and map to the same 400 response.

    Decodes via ``json.detect_encoding`` (the helper ``json.loads`` uses for
    bytes) so BOM-prefixed UTF-8 and UTF-16/UTF-32 bodies keep parsing, since
    pydantic's JSON parser only accepts strict BOM-less UTF-8.
    """
    try:
        text = body.decode(json.detect_encoding(body))
        return ChatCompletionRequest.model_validate_json(text)
    except (UnicodeDecodeError, ValidationError):
        return error_response(400, "Invalid request body")


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


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


def convert_messages(
    messages: list[ChatMessage],
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


def derive_session_id(
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


# ---------------------------------------------------------------------------
# Model-identity resolution
# ---------------------------------------------------------------------------


def openai_compatible_agent_names(config: Config) -> list[str]:
    """Return the configured agents that can be exposed as /v1 models."""
    delegation_closures: dict[str, frozenset[str]] = {}
    return [
        agent_name
        for agent_name in config.agents
        if agent_name != ROUTER_AGENT_NAME
        and not _openai_incompatible_agent_closure(agent_name, config, delegation_closures=delegation_closures)
    ]


def openai_incompatible_agents(agent_names: list[str], config: Config) -> list[str]:
    """Return the requested agents whose delegation closure is unsupported on /v1."""
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

    return error_response(
        400,
        f"{message} Unsupported agents: {invalid_agents}",
        param="model",
        code="unsupported_worker_scope",
    )


def _validate_team_model_request(team_name: str, config: Config) -> JSONResponse | None:
    if not config.teams or team_name not in config.teams:
        return error_response(
            404,
            f"Team '{team_name}' not found",
            param="model",
            code="model_not_found",
        )
    invalid_agents = openai_incompatible_agents(config.teams[team_name].agents, config)
    if invalid_agents:
        return _unsupported_worker_scope_error(invalid_agents, config)
    return None


def _validate_agent_model_request(agent_name: str, config: Config) -> JSONResponse | None:
    if agent_name not in config.agents or agent_name == ROUTER_AGENT_NAME or agent_name in RESERVED_MODEL_NAMES:
        return error_response(
            404,
            f"Model '{agent_name}' not found",
            param="model",
            code="model_not_found",
        )
    invalid_agents = openai_incompatible_agents([agent_name], config)
    if invalid_agents:
        return _unsupported_worker_scope_error(invalid_agents, config)
    return None


def validate_chat_request(
    req: ChatCompletionRequest,
    config: Config,
) -> JSONResponse | None:
    """Validate a chat completion request. Returns error response or None if valid."""
    if not req.messages:
        return error_response(400, "Messages array is required and must not be empty")

    agent_name = req.model

    if agent_name.startswith(TEAM_MODEL_PREFIX):
        return _validate_team_model_request(agent_name.removeprefix(TEAM_MODEL_PREFIX), config)

    if agent_name == AUTO_MODEL_NAME:
        return None  # auto-routing handled in chat_completions

    return _validate_agent_model_request(agent_name, config)
