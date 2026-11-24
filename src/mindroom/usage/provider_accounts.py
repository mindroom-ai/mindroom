"""Billing-account discovery from MindRoom model configuration and runtime paths."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from mindroom.config.schema_hints import ENV_PLACEHOLDER_PATTERN
from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_provider
from mindroom.model_defaults import OPENROUTER_BASE_URL_DEFAULT
from mindroom.providers.credentials import (
    model_api_key,
    model_with_dashboard_key,
    own_api_key,
    resolve_api_key_reference,
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
_CLI_KINDS: tuple[Literal["claude_code", "codex"], ...] = ("claude_code", "codex")
_OPENROUTER_ENV_BASE_URLS = {"openai": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL"}
_OFFICIAL_OPENROUTER_HOSTS = frozenset({"openrouter.ai", "www.openrouter.ai"})


class _Sentinel(Enum):
    MISSING = "missing"
    UNREADABLE = "unreadable"


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


def _client_params(model_config: ModelConfig) -> Mapping[str, object]:
    raw = (model_config.extra_kwargs or {}).get("client_params")
    return raw if isinstance(raw, Mapping) else {}


def _clean_secret(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped and not ENV_PLACEHOLDER_PATTERN.fullmatch(stripped) else None


def _bearer_token(value: object) -> str | None:
    parts = value.strip().split() if isinstance(value, str) else []
    return _clean_secret(parts[1]) if len(parts) == 2 and parts[0].lower() == "bearer" else None


def _is_official_openrouter_host(route: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> bool:
    if route not in {"openrouter", "openai", "anthropic"}:
        return False
    authored = _client_params(model_config).get("base_url") or (model_config.extra_kwargs or {}).get("base_url")
    if isinstance(authored, str) and authored.strip():
        url: str | None = authored.strip()
    elif route == "openrouter":
        url = OPENROUTER_BASE_URL_DEFAULT
    else:
        url = get_secret_from_env(_OPENROUTER_ENV_BASE_URLS[route], runtime_paths=runtime_paths)
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _OFFICIAL_OPENROUTER_HOSTS


def _find_header(layers: tuple[object, ...], header_name: str) -> object:
    target = header_name.lower()
    for layer in layers:
        items = layer.items() if isinstance(layer, Mapping) else (layer if isinstance(layer, list) else ())
        matches = [val for key, val in items if isinstance(key, str) and key.lower() == target]
        if matches:
            return matches[0] if all(val == matches[0] for val in matches[1:]) else _Sentinel.UNREADABLE
    return _Sentinel.MISSING


def _anthropic_env_headers(runtime_paths: RuntimePaths) -> list[tuple[str, str]] | Literal[_Sentinel.UNREADABLE]:
    raw = runtime_paths.process_env.get("ANTHROPIC_CUSTOM_HEADERS")
    if not isinstance(raw, str) or not (stripped := raw.strip()):
        return []
    if ENV_PLACEHOLDER_PATTERN.fullmatch(stripped):
        return _Sentinel.UNREADABLE
    pairs = [(parts[0].strip(), parts[1].strip()) for line in raw.split("\n") if len(parts := line.split(":", 1)) == 2]
    return [(k, v) for k, v in pairs if k]


def _auth_header_token(
    route: str,
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> str | None | Literal[_Sentinel.UNREADABLE]:
    extra, cp = model_config.extra_kwargs or {}, _client_params(model_config)
    req_params = extra.get("request_params")
    req_headers = (
        req_params["extra_headers"]
        if isinstance(req_params, Mapping) and "extra_headers" in req_params
        else extra.get("extra_headers")
    )
    if route == "anthropic":
        default_headers = (
            extra.get("default_headers") if extra.get("default_headers") is not None else cp.get("default_headers")
        )
        env_headers: list[tuple[str, str]] | Literal[_Sentinel.UNREADABLE] = _anthropic_env_headers(runtime_paths)
    else:
        default_headers = cp["default_headers"] if "default_headers" in cp else extra.get("default_headers")
        env_headers = []
    if env_headers is _Sentinel.UNREADABLE:
        return _Sentinel.UNREADABLE
    layers: tuple[object, ...] = (req_headers, default_headers, env_headers)
    found: set[str] = set()
    for name, parser in (("authorization", _bearer_token), ("x-api-key", _clean_secret)):
        raw = _find_header(layers, name)
        if raw is _Sentinel.MISSING:
            continue
        if (token := parser(raw)) is None:
            return _Sentinel.UNREADABLE
        found.add(token)
    return None if not found else (next(iter(found)) if len(found) == 1 else _Sentinel.UNREADABLE)


def _anthropic_auth_token(candidate: ModelConfig, runtime_paths: RuntimePaths) -> str | _Sentinel:
    extra, cp = candidate.extra_kwargs or {}, _client_params(candidate)
    raw = cp.get("auth_token") if "auth_token" in cp else extra.get("auth_token")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return _Sentinel.MISSING
    if isinstance(raw, str) and (match := ENV_PLACEHOLDER_PATTERN.fullmatch(raw.strip())):
        raw = get_secret_from_env(match.group(1), runtime_paths=runtime_paths)
    return token if (token := _clean_secret(raw)) is not None else _Sentinel.UNREADABLE


def _resolve_anthropic_openrouter_key(
    candidate: ModelConfig,
    header_token: str | None,
    runtime_paths: RuntimePaths,
) -> str | None | Literal[_Sentinel.UNREADABLE]:
    auth_token = _anthropic_auth_token(candidate, runtime_paths)
    if header_token is not None:
        return header_token if auth_token in (_Sentinel.MISSING, header_token) else _Sentinel.UNREADABLE
    raw_own = own_api_key(candidate)
    has_own = raw_own is not None and (not isinstance(raw_own, str) or bool(raw_own.strip()))
    own_key = _clean_secret(raw_own)
    if auth_token is not _Sentinel.MISSING:
        return (
            _Sentinel.UNREADABLE
            if (auth_token is _Sentinel.UNREADABLE or (has_own and own_key != auth_token))
            else auth_token
        )
    if any(_client_params(candidate).get(name) is not None for name in ("credentials", "config", "profile")):
        return _Sentinel.UNREADABLE
    if has_own:
        return own_key
    shared = _clean_secret(get_api_key_for_provider("anthropic", runtime_paths=runtime_paths))
    env_tok = _clean_secret(runtime_paths.process_env.get("ANTHROPIC_AUTH_TOKEN"))
    return _Sentinel.UNREADABLE if (shared and env_tok and shared != env_tok) else (env_tok or shared)


def _resolve_openrouter_key(
    route: str,
    model_key: str,
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> str | None | Literal[_Sentinel.UNREADABLE]:
    """Resolve an OpenRouter-hosted model's exact API key, None if missing, or UNREADABLE if ambiguous."""
    header_token = _auth_header_token(route, model_config, runtime_paths)
    if header_token is _Sentinel.UNREADABLE:
        return _Sentinel.UNREADABLE
    candidate = model_config
    with suppress(OSError, ValueError):
        candidate = model_with_dashboard_key(model_key, candidate, runtime_paths)
    candidate = resolve_api_key_reference(candidate, runtime_paths)
    if route == "anthropic":
        return _resolve_anthropic_openrouter_key(candidate, header_token, runtime_paths)
    return header_token if header_token is not None else _clean_secret(model_api_key(candidate, runtime_paths))


def _resolve_cli_scope(cli_path: str | None) -> tuple[str | None, str]:
    effective = cli_path.strip() if isinstance(cli_path, str) and cli_path.strip() else shutil.which("claude")
    if not effective:
        return None, "default"
    if resolved := shutil.which(effective):
        absolute = str(Path(resolved).absolute())
        return absolute, absolute
    return effective, str(Path(effective).expanduser().resolve(strict=False))


def _spec(
    account_id: str,
    provider: str,
    keys: list[str],
    ukeys: set[tuple[str, str]],
    probe_kind: Literal["codex", "claude_code", "openrouter", "payg"],
    scope_key: str = "",
    target: str | None = None,
) -> AccountSpec:
    display = _DISPLAY_OVERRIDES.get(provider, ROUTE_PROVIDERS.get(provider, provider.replace("_", " ").title()))
    return AccountSpec(account_id, provider, display, tuple(keys), frozenset(ukeys), probe_kind, scope_key, target)


def discover_accounts(config: Config, runtime_paths: RuntimePaths) -> list[AccountSpec]:
    """Group configured models into distinct billing accounts with opaque external IDs."""
    env_codex = get_secret_from_env("CODEX_HOME", runtime_paths=runtime_paths)
    defaults = {"claude_code": _resolve_cli_scope(None)[1], "codex": normalize_codex_scope_key(env_codex)}
    grouped: dict[str, dict[str, tuple[str | None, list[str], set[tuple[str, str]]]]] = {
        k: {} for k in ("claude_code", "codex", "openrouter")
    }
    payg: dict[tuple[str, str], tuple[list[str], set[tuple[str, str]]]] = {}

    for model_key in sorted(config.models):
        mc = config.models[model_key]
        route, identity, extra = provider_route(mc.provider), model_identity(mc), mc.extra_kwargs or {}
        if route == "codex":
            raw_home = extra.get("codex_home")
            target = raw_home.strip() if isinstance(raw_home, str) and raw_home.strip() else env_codex
            _, keys, ukeys = grouped["codex"].setdefault(normalize_codex_scope_key(target), (target, [], set()))
        elif route == "claude_code":
            raw_cli = extra.get("cli_path") if isinstance(extra.get("cli_path"), str) else None
            target, scope = _resolve_cli_scope(raw_cli)
            _, keys, ukeys = grouped["claude_code"].setdefault(scope, (target, [], set()))
        elif _is_official_openrouter_host(route, mc, runtime_paths):
            resolved = _resolve_openrouter_key(route, model_key, mc, runtime_paths)
            if resolved is _Sentinel.UNREADABLE:
                keys, ukeys = payg.setdefault(("openrouter", "openrouter:custom"), ([], set()))
            else:
                key_fp = hashlib.sha256(resolved.encode("utf-8")).hexdigest() if resolved else "__missing__"
                _, keys, ukeys = grouped["openrouter"].setdefault(key_fp, (resolved, [], set()))
        else:
            account_id = "openrouter:custom" if route == "openrouter" else f"{route}:default"
            keys, ukeys = payg.setdefault((route, account_id), ([], set()))
        keys.append(model_key)
        ukeys.add((identity.provider, identity.id))

    accounts: list[AccountSpec] = [
        _spec(f"{kind}:default" if i == 0 else f"{kind}:{i + 1}", kind, keys, ukeys, kind, scope, target)
        for kind in _CLI_KINDS
        for i, (scope, (target, keys, ukeys)) in enumerate(
            sorted(grouped[kind].items(), key=lambda item: (item[0] != defaults[kind], item[1][1][0])),
        )
    ]
    accounts.extend(
        _spec(f"openrouter:{i}", "openrouter", keys, ukeys, "openrouter", key_fp, api_key)
        for i, (key_fp, (api_key, keys, ukeys)) in enumerate(
            sorted(grouped["openrouter"].items(), key=lambda item: item[1][1][0]),
            start=1,
        )
    )
    accounts.extend(
        _spec(account_id, route, keys, ukeys, "payg") for (route, account_id), (keys, ukeys) in sorted(payg.items())
    )
    return accounts
