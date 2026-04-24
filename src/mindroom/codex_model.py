"""OpenAI Codex subscription model support via the Codex CLI OAuth state."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from agno.models.openai import OpenAIResponses
from agno.models.response import ModelResponse
from agno.utils.http import get_default_async_client, get_default_sync_client
from openai import AsyncOpenAI, OpenAI

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.models.message import Message
    from agno.run.agent import RunOutput
    from pydantic import BaseModel

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REFRESH_SKEW_SECONDS = 30
CODEX_MODEL_PREFIX = "openai-codex/"
CODEX_DEFAULT_INSTRUCTIONS = "You are a helpful assistant."
CODEX_UNSUPPORTED_REQUEST_PARAMS = {"max_output_tokens"}
CODEX_PROMPT_CACHE_KEY_PREFIX = "mindroom"


class CodexAuthError(ValueError):
    """Raised when the local Codex CLI OAuth state cannot provide a usable token."""


def normalize_codex_model_id(model_id: str) -> str:
    """Return the Codex endpoint model slug from either bare or LLM-plugin-style IDs."""
    normalized = model_id.strip()
    if normalized.startswith(CODEX_MODEL_PREFIX):
        return normalized.removeprefix(CODEX_MODEL_PREFIX)
    return normalized


def borrow_codex_key(*, codex_home: str | Path | None = None) -> tuple[str, str | None]:
    """Return a valid Codex CLI ChatGPT access token and optional account id."""
    auth_path = _codex_auth_path(codex_home=codex_home)
    auth = _read_codex_auth(auth_path)
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        msg = "No ChatGPT access token found in Codex auth.json. Run `codex login` first."
        raise CodexAuthError(msg)

    usable_token = _usable_access_token(tokens)
    if usable_token is not None:
        return usable_token

    with _codex_auth_refresh_lock(auth_path):
        auth = _read_codex_auth(auth_path)
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            msg = "No ChatGPT access token found in Codex auth.json. Run `codex login` first."
            raise CodexAuthError(msg)

        usable_token = _usable_access_token(tokens)
        if usable_token is not None:
            return usable_token

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            msg = "No Codex refresh token found. Run `codex login` to re-authenticate."
            raise CodexAuthError(msg)

        account_id = str(tokens["account_id"]) if tokens.get("account_id") else None
        refreshed = _refresh_codex_tokens(str(refresh_token))
        if not refreshed.get("access_token"):
            msg = "Codex token refresh response did not include an access token."
            raise CodexAuthError(msg)

        _update_tokens(tokens, refreshed)
        auth["tokens"] = tokens
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        _write_codex_auth(auth_path, auth)
        return str(tokens["access_token"]), account_id


def _codex_auth_path(*, codex_home: str | Path | None) -> Path:
    home = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    auth_path = home / "auth.json"
    if not auth_path.exists():
        msg = f"Codex auth file not found at {auth_path}. Run `codex login` first."
        raise CodexAuthError(msg)
    return auth_path


def _read_codex_auth(auth_path: Path) -> dict[str, Any]:
    with auth_path.open(encoding="utf-8") as auth_file:
        auth = json.load(auth_file)
    if auth.get("auth_mode") != "chatgpt":
        msg = "Codex auth.json must use ChatGPT OAuth auth_mode. Run `codex login` first."
        raise CodexAuthError(msg)
    return auth


@contextmanager
def _codex_auth_refresh_lock(auth_path: Path) -> Iterator[None]:
    lock_path = auth_path.with_name(f"{auth_path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _usable_access_token(tokens: dict[str, Any]) -> tuple[str, str | None] | None:
    access_token = str(tokens["access_token"])
    expires_at = _jwt_exp(access_token)
    if expires_at is None or time.time() >= expires_at - CODEX_REFRESH_SKEW_SECONDS:
        return None
    account_id = str(tokens["account_id"]) if tokens.get("account_id") else None
    return access_token, account_id


def _write_codex_auth(auth_path: Path, auth: dict[str, Any]) -> None:
    temp_path = auth_path.with_name(f"{auth_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(auth, indent=2))
    temp_path.replace(auth_path)
    auth_path.chmod(0o600)


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        exp = json.loads(decoded).get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    return int(exp) if isinstance(exp, int) else None


def _refresh_codex_tokens(refresh_token: str) -> dict[str, Any]:
    payload = {
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        response = httpx.post(CODEX_REFRESH_URL, json=payload, timeout=10)
    except httpx.HTTPError as exc:
        msg = f"Codex token refresh failed: {exc}"
        raise CodexAuthError(msg) from exc

    if not response.is_success:
        error_body = response.text
        error_code = _refresh_error_code(error_body)
        if error_code in {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}:
            msg = f"Codex refresh token is no longer valid ({error_code}). Run `codex login` again."
            raise CodexAuthError(msg) from None
        msg = f"Codex token refresh failed (HTTP {response.status_code}): {error_body}"
        raise CodexAuthError(msg) from None

    try:
        return response.json()
    except json.JSONDecodeError as exc:
        msg = "Codex token refresh returned invalid JSON."
        raise CodexAuthError(msg) from exc


def _refresh_error_code(error_body: str) -> str | None:
    try:
        error = json.loads(error_body)
    except json.JSONDecodeError:
        return None
    code = error.get("error")
    return code if isinstance(code, str) else None


def _update_tokens(tokens: dict[str, Any], refreshed: dict[str, Any]) -> None:
    for key in ("access_token", "id_token", "refresh_token"):
        if refreshed.get(key):
            tokens[key] = refreshed[key]


def derive_codex_prompt_cache_key(identity: ToolExecutionIdentity) -> str | None:
    """Derive a stable Codex prompt-cache routing key for one active execution."""
    if identity.session_id is None:
        return None
    source = ":".join(
        (
            identity.channel,
            identity.agent_name,
            identity.requester_id or "",
            identity.room_id or "",
            identity.resolved_thread_id or identity.thread_id or "",
            identity.session_id,
        ),
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"{CODEX_PROMPT_CACHE_KEY_PREFIX}-{digest}"


def _codex_prompt_cache_headers(prompt_cache_key: str) -> dict[str, str]:
    return {
        "session_id": prompt_cache_key,
        "x-client-request-id": prompt_cache_key,
        "x-codex-window-id": f"{prompt_cache_key}:1",
    }


@dataclass
class CodexResponses(OpenAIResponses):
    """Agno Responses model backed by the local Codex CLI ChatGPT OAuth credentials."""

    id: str = "gpt-5.5"
    name: str = "CodexResponses"
    provider: str = "OpenAI Codex"
    base_url: str = CODEX_BASE_URL
    store: bool = False
    codex_home: str | None = None
    prompt_cache_key: str | None = None

    def __post_init__(self) -> None:
        """Normalize LLM-plugin-style model IDs before Agno uses the model id."""
        self.id = normalize_codex_model_id(self.id)
        super().__post_init__()

    def _get_client_params(self) -> dict[str, Any]:
        token, account_id = borrow_codex_key(codex_home=self.codex_home)
        headers = dict(self.default_headers or {})
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id

        base_params = {
            "api_key": token,
            "organization": self.organization,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": headers or None,
            "default_query": self.default_query,
        }
        client_params = {key: value for key, value in base_params.items() if value is not None}
        if self.client_params:
            client_params.update(self.client_params)
        return client_params

    def _instructions_text(self) -> str:
        instructions = [self.system_prompt, *(self.instructions or [])]
        return "\n\n".join(instruction for instruction in instructions if instruction) or CODEX_DEFAULT_INSTRUCTIONS

    def _prompt_cache_key(self) -> str | None:
        return self.prompt_cache_key

    def get_request_params(
        self,
        messages: list[Message] | None = None,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add the top-level instructions field required by the Codex endpoint."""
        request_params = super().get_request_params(
            messages=messages,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
        )
        request_params.setdefault("instructions", self._instructions_text())
        prompt_cache_key = self._prompt_cache_key()
        if prompt_cache_key:
            request_params.setdefault("prompt_cache_key", prompt_cache_key)
            extra_headers = dict(request_params.get("extra_headers") or {})
            for header_name, header_value in _codex_prompt_cache_headers(prompt_cache_key).items():
                extra_headers.setdefault(header_name, header_value)
            request_params["extra_headers"] = extra_headers
        for param_name in CODEX_UNSUPPORTED_REQUEST_PARAMS:
            request_params.pop(param_name, None)
        return request_params

    def invoke(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """Return a normal response by consuming the Codex endpoint's required stream."""
        self._ensure_message_metrics_initialized(assistant_message)
        model_response = ModelResponse(role=self.assistant_message_role)

        for response_delta in self.invoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            _merge_response_delta(model_response, response_delta)

        self._populate_assistant_message(assistant_message, model_response)
        return model_response

    async def ainvoke(
        self,
        messages: list[Message],
        assistant_message: Message,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | None = None,
        compress_tool_results: bool = False,
    ) -> ModelResponse:
        """Return a normal async response by consuming the required Codex stream."""
        self._ensure_message_metrics_initialized(assistant_message)
        model_response = ModelResponse(role=self.assistant_message_role)

        async for response_delta in self.ainvoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
            compress_tool_results=compress_tool_results,
        ):
            _merge_response_delta(model_response, response_delta)

        self._populate_assistant_message(assistant_message, model_response)
        return model_response

    def get_client(self) -> OpenAI:
        """Return a fresh sync client so expired Codex tokens are refreshed between requests."""
        client_params = self._get_client_params()
        client_params["http_client"] = (
            self.http_client if isinstance(self.http_client, httpx.Client) else get_default_sync_client()
        )
        return OpenAI(**client_params)

    def get_async_client(self) -> AsyncOpenAI:
        """Return a fresh async client so expired Codex tokens are refreshed between requests."""
        client_params = self._get_client_params()
        client_params["http_client"] = (
            self.http_client if isinstance(self.http_client, httpx.AsyncClient) else get_default_async_client()
        )
        return AsyncOpenAI(**client_params)


def _append_response_string(existing: str | None, value: str) -> str:
    return value if existing is None else existing + value


def _merge_dict_data(target_data: dict[str, Any] | None, delta_data: dict[str, Any] | None) -> dict[str, Any] | None:
    if delta_data is None:
        return target_data
    if target_data is None:
        target_data = {}
    for key, value in delta_data.items():
        existing = target_data.get(key)
        if isinstance(existing, list) and isinstance(value, list):
            existing.extend(value)
        else:
            target_data[key] = value
    return target_data


def _extend_response_list(existing: list[Any] | None, value: list[Any] | None) -> list[Any] | None:
    if value is None:
        return existing
    if existing is None:
        existing = []
    existing.extend(value)
    return existing


def _merge_response_delta(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    _merge_response_content(model_response, response_delta)
    _merge_response_media(model_response, response_delta)
    _merge_response_tools(model_response, response_delta)
    _merge_response_metadata(model_response, response_delta)
    _merge_response_metrics(model_response, response_delta)


def _merge_response_content(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    if response_delta.role is not None:
        model_response.role = response_delta.role
    if response_delta.content is not None:
        if isinstance(model_response.content, str) and isinstance(response_delta.content, str):
            model_response.content += response_delta.content
        else:
            model_response.content = response_delta.content
    if response_delta.parsed is not None:
        model_response.parsed = response_delta.parsed
    if response_delta.redacted_reasoning_content is not None:
        model_response.redacted_reasoning_content = _append_response_string(
            model_response.redacted_reasoning_content,
            response_delta.redacted_reasoning_content,
        )
    if response_delta.reasoning_content is not None:
        model_response.reasoning_content = _append_response_string(
            model_response.reasoning_content,
            response_delta.reasoning_content,
        )
    if response_delta.citations is not None:
        model_response.citations = response_delta.citations


def _merge_response_media(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    if response_delta.audio is not None:
        model_response.audio = response_delta.audio
    if response_delta.images is not None:
        model_response.images = _extend_response_list(model_response.images, response_delta.images)
    if response_delta.videos is not None:
        model_response.videos = _extend_response_list(model_response.videos, response_delta.videos)
    if response_delta.audios is not None:
        model_response.audios = _extend_response_list(model_response.audios, response_delta.audios)
    if response_delta.files is not None:
        model_response.files = _extend_response_list(model_response.files, response_delta.files)


def _merge_response_tools(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    if response_delta.tool_calls:
        model_response.tool_calls.extend(response_delta.tool_calls)
    if response_delta.tool_executions:
        if model_response.tool_executions is None:
            model_response.tool_executions = []
        model_response.tool_executions.extend(response_delta.tool_executions)


def _merge_response_metadata(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    if response_delta.provider_data is not None:
        model_response.provider_data = _merge_dict_data(model_response.provider_data, response_delta.provider_data)
    if response_delta.extra is not None:
        model_response.extra = _merge_dict_data(model_response.extra, response_delta.extra)
    if response_delta.updated_session_state is not None:
        model_response.updated_session_state = _merge_dict_data(
            model_response.updated_session_state,
            response_delta.updated_session_state,
        )
    if response_delta.compression_stats is not None:
        model_response.compression_stats = response_delta.compression_stats


def _merge_response_metrics(model_response: ModelResponse, response_delta: ModelResponse) -> None:
    if response_delta.response_usage is not None:
        model_response.response_usage = response_delta.response_usage
    if response_delta.input_tokens is not None:
        model_response.input_tokens = response_delta.input_tokens
    if response_delta.output_tokens is not None:
        model_response.output_tokens = response_delta.output_tokens
    if response_delta.total_tokens is not None:
        model_response.total_tokens = response_delta.total_tokens
    if response_delta.time_to_first_token is not None:
        model_response.time_to_first_token = response_delta.time_to_first_token
    if response_delta.reasoning_tokens is not None:
        model_response.reasoning_tokens = response_delta.reasoning_tokens
    if response_delta.cache_read_tokens is not None:
        model_response.cache_read_tokens = response_delta.cache_read_tokens
    if response_delta.cache_write_tokens is not None:
        model_response.cache_write_tokens = response_delta.cache_write_tokens
