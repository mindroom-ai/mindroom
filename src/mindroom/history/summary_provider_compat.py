"""Provider/SDK workarounds for one dedicated portable-summary model.

Keep raw-body precedence, cached SDK clients, and provider completion semantics here.
The summary engine consumes the resulting request settings without knowing the
provider's configuration layers or transport internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import httpx

from mindroom.claude_prompt_cache import as_anthropic_claude

if TYPE_CHECKING:
    from agno.models.anthropic import Claude
    from agno.models.base import Model
    from agno.models.response import ModelResponse


def _authored_timeouts(model: Claude) -> tuple[object, ...]:
    return (
        model.timeout,
        (model.client_params or {}).get("timeout"),
        (model.request_params or {}).get("timeout"),
        *(client.timeout for client in (model.client, model.async_client) if client is not None),
        *(
            client.timeout
            for client in (model.http_client, (model.client_params or {}).get("http_client"))
            if isinstance(client, httpx.Client | httpx.AsyncClient)
        ),
    )


def effective_summary_timeout_seconds(model: Model, *, timeout_seconds: float) -> float:
    """Keep a stricter scalar provider deadline within the compaction deadline.

    An HTTP connect/read/write/pool timeout is a phase limit, not a total call
    deadline; those limits are preserved separately during SDK configuration.
    """
    claude = as_anthropic_claude(model)
    limits = _authored_timeouts(claude) if claude is not None else ()
    return min([timeout_seconds, *(value for value in limits if isinstance(value, int | float) and value > 0)])


def _http_timeout(model: Claude, timeout_seconds: float) -> httpx.Timeout:
    phases = {"connect": timeout_seconds, "read": timeout_seconds, "write": timeout_seconds, "pool": timeout_seconds}
    for value in _authored_timeouts(model):
        if isinstance(value, httpx.Timeout):
            for phase, limit in value.as_dict().items():
                if limit is not None:
                    phases[phase] = min(phases[phase], limit)
    return httpx.Timeout(**phases)


def configure_summary_model(model: Model, *, timeout_seconds: float) -> Model:
    """Normalize the effective request on a fresh summary model, preserving caller mappings."""
    from agno.models.openai import OpenAIChat, OpenAIResponses  # noqa: PLC0415 - defer optional provider imports

    # Agno-level retries belong to the outer summary retry policy.
    model.retries = 0
    if isinstance(model, OpenAIChat | OpenAIResponses):
        model.max_retries = 0
        model.client_params = {**(model.client_params or {}), "max_retries": 0}
        if model.client is not None:
            model.client = model.client.with_options(max_retries=0)
        if model.async_client is not None:
            model.async_client = model.async_client.with_options(max_retries=0)
        return model
    claude = as_anthropic_claude(model)
    if claude is None:
        return model
    deadline = effective_summary_timeout_seconds(model, timeout_seconds=timeout_seconds)
    transport_timeout = _http_timeout(claude, deadline)
    params = dict(claude.request_params or {})
    extra_body = dict(params.get("extra_body") or {})
    claude.max_tokens = extra_body.pop("max_tokens", params.pop("max_tokens", claude.max_tokens))
    params.pop("thinking", None)
    extra_body.pop("thinking", None)
    if extra_body:
        params["extra_body"] = extra_body
    else:
        params.pop("extra_body", None)
    params["timeout"] = transport_timeout
    claude.request_params = params
    claude.thinking = None
    claude.cache_system_prompt = False
    claude.extended_cache_time = False
    claude.timeout = deadline
    claude.client_params = {**(claude.client_params or {}), "max_retries": 0, "timeout": transport_timeout}
    # Agno can hold injected/cached clients. Copy options, preserving both the
    # caller's client and its transport. Mantle's with_options currently drops
    # the transport unless this private SDK field is passed explicitly.
    if claude.client is not None:
        claude.client = claude.client.with_options(
            timeout=transport_timeout,
            max_retries=0,
            http_client=claude.client._client,
        )
    if claude.async_client is not None:
        claude.async_client = claude.async_client.with_options(
            timeout=transport_timeout,
            max_retries=0,
            http_client=claude.async_client._client,
        )
    return model


def summary_output_token_limit(model: Model) -> int | None:
    """Read the output cap after effective request settings have been normalized."""
    claude = as_anthropic_claude(model)
    return claude.max_tokens if claude is not None else None


def summary_completion_status(
    response: ModelResponse,
    *,
    output_token_limit: int | None,
) -> Literal["complete", "output_limit", "incomplete"]:
    """Normalize provider completion signals, with a conservative legacy usage fallback."""
    data = response.provider_data or {}
    reason = data.get("stop_reason")
    if reason is not None:
        return "output_limit" if reason in {"max_tokens", "model_context_window_exceeded"} else "complete"
    finish_reason = data.get("finish_reason")
    if finish_reason is not None:
        if finish_reason == "length":
            return "output_limit"
        return "complete" if finish_reason == "stop" else "incomplete"
    status = data.get("response_status")
    if status is not None:
        if status == "completed":
            return "complete"
        return "output_limit" if data.get("incomplete_reason") == "max_output_tokens" else "incomplete"
    output_tokens = response_output_tokens(response)
    return (
        "output_limit"
        if output_token_limit is not None and output_tokens is not None and output_tokens >= output_token_limit
        else "complete"
    )


def response_output_tokens(response: ModelResponse) -> int | None:
    """Read output usage from the two response shapes exposed by Agno."""
    if response.output_tokens is not None:
        return response.output_tokens
    return response.response_usage.output_tokens if response.response_usage is not None else None
