"""Billing-account discovery from MindRoom model configuration and runtime paths."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from mindroom.config.schema_hints import ENV_PLACEHOLDER_PATTERN
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.model_defaults import ANTHROPIC_BASE_URL_DEFAULT, OPENAI_BASE_URL_DEFAULT, OPENROUTER_BASE_URL_DEFAULT
from mindroom.providers.credentials import (
    model_api_key,
    model_with_dashboard_key,
    own_api_key,
    resolve_api_key_reference,
    uses_alternative_auth,
)
from mindroom.providers.identity import model_identity
from mindroom.providers.names import ROUTE_PROVIDERS, provider_route
from mindroom.usage.limit_store import normalize_codex_scope_key

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths

__all__ = ["AccountSpec", "discover_accounts"]

_DISPLAY_OVERRIDES = {
    "claude_code": "Claude Code",
    "codex": "Codex",
    "bedrock_claude": "AWS Bedrock",
    "vertexai_claude": "Vertex AI",
    "llama_cpp": "llama.cpp",
    "zai": "Z.AI",
}


@dataclass(frozen=True, slots=True)
class AccountSpec:
    """Resolved billing account grouping (opaque external ID plus in-memory probe target)."""

    id: str
    provider: str
    display_name: str
    model_keys: tuple[str, ...]
    usage_keys: frozenset[tuple[str, str]]
    probe_kind: Literal["codex", "claude_code", "openrouter", "payg"]
    scope_key: str = ""
    target: str | None = None


def _is_official_openrouter_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {"openrouter.ai", "www.openrouter.ai"}


def _effective_base_url(route: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    extra = model_config.extra_kwargs or {}
    client_params = extra.get("client_params") if isinstance(extra.get("client_params"), Mapping) else {}
    if isinstance(authored := (client_params.get("base_url") or extra.get("base_url")), str) and authored.strip():
        return authored.strip()
    if route == "openrouter":
        return OPENROUTER_BASE_URL_DEFAULT
    env_defaults = {
        "openai": ("OPENAI_BASE_URL", OPENAI_BASE_URL_DEFAULT),
        "anthropic": ("ANTHROPIC_BASE_URL", ANTHROPIC_BASE_URL_DEFAULT),
    }
    if route in env_defaults:
        return get_secret_from_env(env_defaults[route][0], runtime_paths=runtime_paths) or env_defaults[route][1]
    return None


def _resolve_openrouter_key(model_key: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    candidate = model_config
    with suppress(OSError, ValueError):
        candidate = model_with_dashboard_key(model_key, candidate, runtime_paths)
    candidate = resolve_api_key_reference(candidate, runtime_paths)
    if provider_route(candidate.provider) == "anthropic":
        key = own_api_key(candidate)
        if not key and isinstance(token := (candidate.extra_kwargs or {}).get("auth_token"), str) and token.strip():
            match = ENV_PLACEHOLDER_PATTERN.fullmatch(token.strip())
            key = get_secret_from_env(match.group(1), runtime_paths=runtime_paths) if match else token.strip()
        if not key and not uses_alternative_auth(candidate, runtime_paths):
            key = get_api_key_for_provider("anthropic", runtime_paths=runtime_paths)
    else:
        key = model_api_key(candidate, runtime_paths)
    if not isinstance(key, str) or not key.strip() or ENV_PLACEHOLDER_PATTERN.fullmatch(key.strip()):
        return None
    return key.strip()


def _resolve_cli_scope(cli_path: str | None) -> tuple[str | None, str]:
    effective = cli_path.strip() if isinstance(cli_path, str) and cli_path.strip() else shutil.which("claude")
    if not effective:
        return None, "default"
    if resolved := shutil.which(effective):
        absolute = str(Path(resolved).absolute())
        return absolute, absolute
    return effective, str(Path(effective).expanduser().resolve(strict=False))


def _display_name(route: str) -> str:
    return _DISPLAY_OVERRIDES.get(route, ROUTE_PROVIDERS.get(route, route.replace("_", " ").title()))


def discover_accounts(config: Config, runtime_paths: RuntimePaths) -> list[AccountSpec]:
    """Group configured models into distinct billing accounts with opaque external IDs."""
    env_codex = get_secret_from_env("CODEX_HOME", runtime_paths=runtime_paths)
    defaults = {"claude_code": _resolve_cli_scope(None)[1], "codex": normalize_codex_scope_key(env_codex)}
    grouped: dict[str, dict[str, tuple[str | None, list[str], set[tuple[str, str]]]]] = {
        k: {} for k in ("claude_code", "codex", "openrouter", "payg")
    }
    for model_key in sorted(config.models):
        model_config = config.models[model_key]
        route, identity, extra = (
            provider_route(model_config.provider),
            model_identity(model_config),
            model_config.extra_kwargs or {},
        )
        if route == "codex":
            raw_home = extra.get("codex_home")
            target = raw_home.strip() if isinstance(raw_home, str) and raw_home.strip() else env_codex
            bucket, key = "codex", normalize_codex_scope_key(target)
        elif route == "claude_code":
            target, key = _resolve_cli_scope(extra.get("cli_path") if isinstance(extra.get("cli_path"), str) else None)
            bucket = "claude_code"
        elif route in {"openrouter", "openai", "anthropic"} and _is_official_openrouter_url(
            _effective_base_url(route, model_config, runtime_paths),
        ):
            target = _resolve_openrouter_key(model_key, model_config, runtime_paths)
            bucket, key = "openrouter", hashlib.sha256(target.encode()).hexdigest() if target else "__missing__"
        else:
            bucket, key, target = "payg", (f"{route}:custom" if route == "openrouter" else f"{route}:default"), route
        _, keys, ukeys = grouped[bucket].setdefault(key, (target, [], set()))
        keys.append(model_key)
        ukeys.add((identity.provider, identity.id))

    accounts: list[AccountSpec] = []
    for kind in ("claude_code", "codex"):
        items = sorted(grouped[kind].items(), key=lambda item: (item[0] != defaults[kind], item[1][1][0]))
        accounts.extend(
            AccountSpec(
                id=f"{kind}:default" if i == 0 else f"{kind}:{i + 1}",
                provider=kind,
                display_name=_display_name(kind),
                model_keys=tuple(keys),
                usage_keys=frozenset(ukeys),
                probe_kind=kind,
                scope_key=scope,
                target=target,
            )
            for i, (scope, (target, keys, ukeys)) in enumerate(items)
        )
    accounts.extend(
        AccountSpec(
            id=f"openrouter:{i}",
            provider="openrouter",
            display_name=_display_name("openrouter"),
            model_keys=tuple(keys),
            usage_keys=frozenset(ukeys),
            probe_kind="openrouter",
            target=api_key,
        )
        for i, (_, (api_key, keys, ukeys)) in enumerate(
            sorted(grouped["openrouter"].items(), key=lambda item: item[1][1][0]),
            start=1,
        )
    )
    accounts.extend(
        AccountSpec(
            id=aid,
            provider=route or "unknown",
            display_name=_display_name(route or "unknown"),
            model_keys=tuple(keys),
            usage_keys=frozenset(ukeys),
            probe_kind="payg",
        )
        for aid, (route, keys, ukeys) in sorted(grouped["payg"].items())
    )
    return accounts
