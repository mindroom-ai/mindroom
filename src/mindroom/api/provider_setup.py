"""First-run AI provider setup for the dashboard.

Reports which provider keys the configured router, agents, and teams still need,
and saves a pasted provider key only after the provider accepts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mindroom.api import config_lifecycle
from mindroom.api.credentials import save_dashboard_api_key
from mindroom.logging_config import get_logger
from mindroom.providers.key_checks import PROVIDER_KEY_CHECKS
from mindroom.providers.model_loading import missing_model_api_key_provider

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

router = APIRouter(prefix="/api/provider-setup", tags=["provider-setup"])

_ConnectableProvider = Literal["openrouter", "anthropic", "openai"]

_LABELS: dict[_ConnectableProvider, str] = {"openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI"}

_KEY_CHECK_TIMEOUT_SECONDS = 10.0


class MissingProviderKey(BaseModel):
    """A provider the configured entities use without a resolvable API key."""

    provider: str
    models: list[str]


class ProviderSetupStatus(BaseModel):
    """Provider keys the configured router, agents, and teams still need."""

    missing: list[MissingProviderKey]


class ConnectProviderRequest(BaseModel):
    """A pasted provider API key to verify and save."""

    provider: _ConnectableProvider
    api_key: str


class ConnectProviderResponse(BaseModel):
    """Result of saving a verified provider key."""

    service: str
    missing: list[MissingProviderKey]


def _entity_model_names(config: Config) -> list[str]:
    """Return the model names used by the router, agents, and teams, in first-use order."""
    names = [config.router.model]
    names.extend(agent.model for agent in config.agents.values())
    names.extend(team.model for team in config.teams.values() if team.model is not None)
    return list(dict.fromkeys(names))


def _missing_provider_keys(config: Config, runtime_paths: RuntimePaths) -> list[MissingProviderKey]:
    models_by_provider: dict[str, list[str]] = {}
    for model_name in _entity_model_names(config):
        provider = missing_model_api_key_provider(config, runtime_paths, model_name)
        if provider is not None:
            models_by_provider.setdefault(provider, []).append(model_name)
    return [MissingProviderKey(provider=provider, models=models) for provider, models in models_by_provider.items()]


async def _verify_provider_key(provider: _ConnectableProvider, api_key: str) -> None:
    """Raise an HTTP error unless the provider accepts the key. Never logs or echoes the key."""
    label = _LABELS[provider]
    url, headers = PROVIDER_KEY_CHECKS[provider].request(api_key)
    try:
        async with httpx.AsyncClient(timeout=_KEY_CHECK_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("provider_key_check_unreachable", provider=provider, error_type=type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach {label} to verify the key. Try again in a moment.",
        ) from None
    if response.status_code in {401, 403}:
        logger.info("provider_key_check_rejected", provider=provider, status_code=response.status_code)
        raise HTTPException(
            status_code=400,
            detail=f"{label} rejected this API key. Check that you copied the whole key and that it is active.",
        )
    if response.is_error:
        logger.warning("provider_key_check_failed", provider=provider, status_code=response.status_code)
        raise HTTPException(
            status_code=502,
            detail=f"{label} could not verify the key right now (HTTP {response.status_code}). Try again shortly.",
        )


@router.get("/status")
async def get_provider_setup_status(request: Request) -> ProviderSetupStatus:
    """Report provider keys that the configured router, agents, and teams cannot resolve."""
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    return ProviderSetupStatus(missing=_missing_provider_keys(config, runtime_paths))


@router.post("/connect")
async def connect_provider(request: Request, payload: ConnectProviderRequest) -> ConnectProviderResponse:
    """Verify a provider key with the provider, then save it under the canonical provider service."""
    api_key = payload.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="Paste an API key first.")
    await _verify_provider_key(payload.provider, api_key)
    service = save_dashboard_api_key(request, payload.provider, api_key)
    logger.info("provider_key_connected", provider=payload.provider)
    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    return ConnectProviderResponse(
        service=service,
        missing=_missing_provider_keys(config, runtime_paths),
    )
