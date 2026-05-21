"""OpenRouter API key provisioning."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/keys"

HttpPost = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter key provisioning fails."""


class OpenRouterConfigurationError(OpenRouterError):
    """Raised when local OpenRouter provisioning configuration is missing."""


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


def _default_http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        msg = "OpenRouter key creation failed before receiving a response"
        raise OpenRouterError(msg) from exc


def create_openrouter_key(
    *,
    management_api_key: str,
    plan: OpenRouterKeyPlan,
    http_post: HttpPost = _default_http_post,
) -> CreatedOpenRouterKey:
    """Create a monthly spending-limited OpenRouter API key."""
    if not management_api_key.strip():
        msg = "OPENROUTER_PROVISIONING_API_KEY is required to create included-budget OpenRouter keys"
        raise OpenRouterConfigurationError(msg)
    if plan.monthly_limit_usd <= 0:
        msg = "OpenRouter monthly_limit_usd must be greater than 0"
        raise OpenRouterError(msg)

    body = json.dumps(
        {
            "name": plan.name,
            "limit": plan.monthly_limit_usd,
            "limit_reset": "monthly",
            "include_byok_in_limit": True,
        }
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {management_api_key.strip()}",
        "Content-Type": "application/json",
    }

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
