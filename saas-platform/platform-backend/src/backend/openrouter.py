"""OpenRouter API key provisioning."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/keys"

HttpPost = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]
HttpDelete = Callable[[str, dict[str, str]], tuple[int, bytes]]
HttpPatch = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter key provisioning fails."""


class OpenRouterConfigurationError(OpenRouterError):
    """Raised when local OpenRouter provisioning configuration is missing."""


class OpenRouterKeyNotFoundError(OpenRouterError):
    """Raised when OpenRouter reports that a key hash no longer exists."""


@dataclass(frozen=True)
class OpenRouterKeyPlan:
    """Inputs for a monthly-limited OpenRouter key."""

    name: str
    monthly_limit_usd: int


@dataclass(frozen=True)
class CreatedOpenRouterKey:
    """OpenRouter key material and non-secret metadata."""

    key: str
    hash: str
    label: str
    limit_usd: int
    limit_reset: str


def _send_http_request(method: str, url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        msg = f"OpenRouter key {method} request failed before receiving a response"
        raise OpenRouterError(msg) from exc


def _default_http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    return _send_http_request("POST", url, headers, body)


def _default_http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    return _send_http_request("DELETE", url, headers, b"{}")


def _default_http_patch(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    return _send_http_request("PATCH", url, headers, body)


def create_openrouter_key(
    *, management_api_key: str, plan: OpenRouterKeyPlan, http_post: HttpPost = _default_http_post
) -> CreatedOpenRouterKey:
    """Create a monthly spending-limited OpenRouter API key."""
    if not management_api_key.strip():
        msg = "OPENROUTER_PROVISIONING_API_KEY is required to create included-budget OpenRouter keys"
        raise OpenRouterConfigurationError(msg)
    if plan.monthly_limit_usd <= 0:
        msg = "OpenRouter monthly_limit_usd must be greater than 0"
        raise OpenRouterError(msg)

    body = json.dumps(
        {"name": plan.name, "limit": plan.monthly_limit_usd, "limit_reset": "monthly", "include_byok_in_limit": True}
    ).encode("utf-8")
    headers = {"Authorization": f"Bearer {management_api_key.strip()}", "Content-Type": "application/json"}

    status, response_body = http_post(OPENROUTER_KEYS_URL, headers, body)
    if status != 201:
        error_detail = response_body.decode("utf-8", errors="replace")
        msg = f"OpenRouter key creation failed with status {status}: {error_detail}"
        raise OpenRouterError(msg)

    try:
        payload = json.loads(response_body.decode("utf-8"))
        data = payload["data"]
        return CreatedOpenRouterKey(
            key=payload["key"],
            hash=data["hash"],
            label=data["label"],
            limit_usd=int(data["limit"]),
            limit_reset=data["limit_reset"],
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        msg = "Failed to decode OpenRouter key creation response"
        raise OpenRouterError(msg) from exc
    except KeyError as exc:
        msg = f"OpenRouter key creation response is missing field: {exc.args[0]}"
        raise OpenRouterError(msg) from exc
    except (TypeError, ValueError) as exc:
        msg = "OpenRouter key creation response has invalid field values"
        raise OpenRouterError(msg) from exc


def _key_request_target(management_api_key: str, key_hash: str, action: str) -> tuple[str, dict[str, str]]:
    """Validate management inputs and return the key URL and headers for one key operation."""
    if not isinstance(management_api_key, str):
        msg = "OPENROUTER_PROVISIONING_API_KEY must be a string"
        raise OpenRouterConfigurationError(msg)
    management_api_key = management_api_key.strip()
    if not management_api_key:
        msg = f"OPENROUTER_PROVISIONING_API_KEY is required to {action} OpenRouter keys"
        raise OpenRouterConfigurationError(msg)
    if not isinstance(key_hash, str):
        msg = "OpenRouter key_hash must be a string"
        raise OpenRouterError(msg)
    key_hash = key_hash.strip()
    if not key_hash:
        msg = f"OpenRouter key_hash is required to {action} OpenRouter keys"
        raise OpenRouterError(msg)

    quoted_hash = urllib.parse.quote(key_hash, safe="")
    headers = {"Authorization": f"Bearer {management_api_key}", "Content-Type": "application/json"}
    return f"{OPENROUTER_KEYS_URL}/{quoted_hash}", headers


def _raise_for_key_status(status: int, response_body: bytes, action: str) -> None:
    if status == 200:
        return
    error_detail = response_body.decode("utf-8", errors="replace")
    msg = f"OpenRouter key {action} failed with status {status}: {error_detail}"
    if status == 404:
        raise OpenRouterKeyNotFoundError(msg)
    raise OpenRouterError(msg)


def delete_openrouter_key(
    *, management_api_key: str, key_hash: str, http_delete: HttpDelete = _default_http_delete
) -> None:
    """Delete an OpenRouter API key by hash."""
    url, headers = _key_request_target(management_api_key, key_hash, "delete")
    status, response_body = http_delete(url, headers)
    _raise_for_key_status(status, response_body, "deletion")

    decoded_body = response_body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(decoded_body)
    except json.JSONDecodeError as exc:
        msg = f"OpenRouter key deletion response is invalid JSON: {decoded_body}"
        raise OpenRouterError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"OpenRouter key deletion response has invalid response type: {decoded_body}"
        raise OpenRouterError(msg)
    if payload.get("deleted") is not True:
        msg = f"OpenRouter key deletion response did not confirm deletion: {decoded_body}"
        raise OpenRouterError(msg)


def set_openrouter_key_disabled(
    *, management_api_key: str, key_hash: str, disabled: bool, http_patch: HttpPatch = _default_http_patch
) -> None:
    """Disable or re-enable an OpenRouter API key by hash without deleting it."""
    url, headers = _key_request_target(management_api_key, key_hash, "update")
    status, response_body = http_patch(url, headers, json.dumps({"disabled": disabled}).encode("utf-8"))
    _raise_for_key_status(status, response_body, "update")


def set_openrouter_key_limit(
    *, management_api_key: str, key_hash: str, limit_usd: int, http_patch: HttpPatch = _default_http_patch
) -> None:
    """Update a key's spending limit without resetting its accumulated usage."""
    url, headers = _key_request_target(management_api_key, key_hash, "update")
    status, response_body = http_patch(url, headers, json.dumps({"limit": limit_usd}).encode("utf-8"))
    _raise_for_key_status(status, response_body, "update")
