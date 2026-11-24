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
    gemini_uses_vertexai,
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


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _client_params(model_config: ModelConfig) -> Mapping[str, object]:
    extra = model_config.extra_kwargs or {}
    raw = extra.get("client_params")
    return raw if isinstance(raw, Mapping) else {}


def _clean_secret(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or ENV_PLACEHOLDER_PATTERN.fullmatch(stripped):
        return None
    return stripped


def _anthropic_env_headers(runtime_paths: RuntimePaths) -> tuple[bool, list[tuple[str, str]]]:
    """Parse process-env ``ANTHROPIC_CUSTOM_HEADERS`` (``Name: Value`` lines); return ``(unreadable, pairs)``."""
    raw_env = runtime_paths.process_env.get("ANTHROPIC_CUSTOM_HEADERS")
    if not isinstance(raw_env, str) or not raw_env.strip():
        return False, []
    if ENV_PLACEHOLDER_PATTERN.fullmatch(raw_env.strip()):
        return True, []
    pairs: list[tuple[str, str]] = []
    for line in raw_env.split("\n"):
        colon = line.find(":")
        if colon < 0:
            continue
        name = line[:colon].strip()
        if name:
            pairs.append((name, line[colon + 1 :].strip()))
    return False, pairs


def _effective_header_layers(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[bool, tuple[object, ...]]:
    """Return ``(unreadable_env, header_layers)`` in route merge precedence order."""
    extra = model_config.extra_kwargs or {}
    request_params = extra.get("request_params")
    req_headers = (
        request_params["extra_headers"]
        if isinstance(request_params, Mapping) and "extra_headers" in request_params
        else extra.get("extra_headers")
    )
    client_params = _client_params(model_config)
    if provider_route(model_config.provider) == "anthropic":
        default_headers = (
            extra.get("default_headers")
            if extra.get("default_headers") is not None
            else client_params.get("default_headers")
        )
        unreadable_env, env_headers = _anthropic_env_headers(runtime_paths)
        return unreadable_env, (req_headers, default_headers, env_headers)
    default_headers = (
        client_params["default_headers"] if "default_headers" in client_params else extra.get("default_headers")
    )
    return False, (req_headers, default_headers)


def _header_override(
    layers: tuple[object, ...],
    header_name: str,
) -> tuple[Literal["none", "value", "conflict"], object]:
    """Return ``(state, value)`` for ``header_name`` across effective request, client, and env header layers."""
    target = header_name.lower()
    for raw_headers in layers:
        if isinstance(raw_headers, Mapping):
            items = list(raw_headers.items())
        elif isinstance(raw_headers, list):
            items = raw_headers
        else:
            continue
        matches = [value for key, value in items if isinstance(key, str) and key.lower() == target]
        if not matches:
            continue
        first = matches[0]
        if all(value == first for value in matches[1:]):
            return "value", first
        return "conflict", tuple(matches)
    return "none", None


def _parse_bearer_header(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    parts = raw_value.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and not ENV_PLACEHOLDER_PATTERN.fullmatch(parts[1]):
        return parts[1]
    return None


def _parse_x_api_key_header(raw_value: object) -> str | None:
    return _clean_secret(raw_value)


def _auth_header_override(
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[Literal["none", "token", "opaque"], str | None, str]:
    """Resolve ``Authorization`` and ``X-Api-Key`` overrides; reject unreadable or conflicting headers."""
    unreadable_env, layers = _effective_header_layers(model_config, runtime_paths)
    if unreadable_env:
        return "opaque", None, "anthropic-custom-headers:unresolved"
    auth_state, auth_raw = _header_override(layers, "authorization")
    xkey_state, xkey_raw = _header_override(layers, "x-api-key")
    if auth_state == "none" and xkey_state == "none":
        return "none", None, ""
    auth_tok = _parse_bearer_header(auth_raw) if auth_state == "value" else None
    xkey_tok = _parse_x_api_key_header(xkey_raw) if xkey_state == "value" else None
    opaque_seed: str | None = None
    if auth_state == "conflict" or xkey_state == "conflict":
        opaque_seed = f"conflict:{auth_state}:{auth_raw!r}:{xkey_state}:{xkey_raw!r}"
    elif auth_state == "value" and auth_tok is None:
        opaque_seed = f"authorization:{auth_raw!r}"
    elif xkey_state == "value" and xkey_tok is None:
        opaque_seed = f"x-api-key:{xkey_raw!r}"
    elif auth_tok is not None and xkey_tok is not None and auth_tok != xkey_tok:
        opaque_seed = f"both:{auth_raw!r}:{xkey_raw!r}"
    if opaque_seed is not None:
        return "opaque", None, opaque_seed
    return "token", (auth_tok if auth_tok is not None else xkey_tok), ""


def _is_official_openrouter_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in {"openrouter.ai", "www.openrouter.ai"}


def _effective_base_url(route: str, model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    extra = model_config.extra_kwargs or {}
    client_params = _client_params(model_config)
    authored = client_params.get("base_url") or extra.get("base_url")
    if isinstance(authored, str) and authored.strip():
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


def _resolve_anthropic_auth_token(candidate: ModelConfig, runtime_paths: RuntimePaths) -> tuple[bool, str | None]:
    extra = candidate.extra_kwargs or {}
    client_params = _client_params(candidate)
    raw = client_params.get("auth_token") if "auth_token" in client_params else extra.get("auth_token")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False, None
    if isinstance(raw, str) and (match := ENV_PLACEHOLDER_PATTERN.fullmatch(raw.strip())):
        return True, _clean_secret(get_secret_from_env(match.group(1), runtime_paths=runtime_paths))
    return True, _clean_secret(raw)


def _resolve_anthropic_credential(
    candidate: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[Literal["none", "token", "opaque"], str | None, str]:
    client_params = _client_params(candidate)
    raw_own_key = own_api_key(candidate)
    has_own_key = raw_own_key is not None and (not isinstance(raw_own_key, str) or bool(raw_own_key.strip()))
    own_key = _clean_secret(raw_own_key)
    has_auth_token, auth_token = _resolve_anthropic_auth_token(candidate, runtime_paths)
    sdk_providers = tuple(name for name in ("credentials", "config", "profile") if client_params.get(name) is not None)
    if has_auth_token:
        if auth_token is None or (has_own_key and own_key != auth_token):
            seed = (
                "anthropic-auth-token:unresolved"
                if auth_token is None
                else f"anthropic-key-and-token:{own_key!r}:{auth_token!r}"
            )
            return "opaque", None, seed
        return "token", auth_token, ""
    if sdk_providers:
        return "opaque", None, f"anthropic-sdk-provider:{','.join(sdk_providers)}"
    if has_own_key:
        return ("token", own_key, "") if own_key is not None else ("none", None, "")
    shared_key = _clean_secret(get_api_key_for_provider("anthropic", runtime_paths=runtime_paths))
    env_auth_token = _clean_secret(runtime_paths.process_env.get("ANTHROPIC_AUTH_TOKEN"))
    if shared_key and env_auth_token and shared_key != env_auth_token:
        return "opaque", None, "anthropic-env-key-and-auth-token"
    token = env_auth_token or shared_key
    return ("token", token, "") if token is not None else ("none", None, "")


def _resolve_model_credential(
    model_key: str,
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> tuple[Literal["none", "token", "opaque"], str | None, str]:
    """Resolve a model's effective token or mark it opaque when headers/SDK auth overrides conflict."""
    auth_kind, header_token, opaque_seed = _auth_header_override(model_config, runtime_paths)
    if auth_kind == "opaque":
        return "opaque", None, opaque_seed
    candidate = model_config
    with suppress(OSError, ValueError):
        candidate = model_with_dashboard_key(model_key, candidate, runtime_paths)
    candidate = resolve_api_key_reference(candidate, runtime_paths)
    if auth_kind == "token" and header_token is not None:
        if provider_route(candidate.provider) == "anthropic":
            has_auth_token, auth_token = _resolve_anthropic_auth_token(candidate, runtime_paths)
            if has_auth_token and auth_token != header_token:
                return "opaque", None, f"anthropic-header-and-token:{header_token!r}:{auth_token!r}"
        return "token", header_token, ""
    if provider_route(candidate.provider) == "anthropic":
        return _resolve_anthropic_credential(candidate, runtime_paths)
    key = _clean_secret(model_api_key(candidate, runtime_paths))
    return ("token", key, "") if key is not None else ("none", None, "")


def _google_vertex_project_id(model_config: ModelConfig, runtime_paths: RuntimePaths) -> str | None:
    """Return the effective Vertex AI project ID for a keyless ``google`` model, if any."""
    if provider_route(model_config.provider) != "google" or not gemini_uses_vertexai(model_config, runtime_paths):
        return None
    extra = model_config.extra_kwargs or {}
    client_params = _client_params(model_config)
    for candidate in (
        client_params.get("project"),
        extra.get("project_id"),
        get_secret_from_env("GOOGLE_CLOUD_PROJECT", runtime_paths=runtime_paths),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _payg_credential_fingerprint(
    model_key: str,
    model_config: ModelConfig,
    runtime_paths: RuntimePaths,
) -> str:
    """Return an in-memory credential fingerprint for grouping PAYG models by billing account."""
    cred_kind, token, opaque_seed = _resolve_model_credential(model_key, model_config, runtime_paths)
    if cred_kind == "token" and token is not None:
        return _digest(token)
    if cred_kind == "opaque":
        return _digest(f"opaque:{opaque_seed}")
    if vertex_project := _google_vertex_project_id(model_config, runtime_paths):
        return _digest(f"vertex-project:{vertex_project}")
    return "__default__"


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


def _payg_specs(
    payg_groups: dict[tuple[str, bool], dict[str, tuple[list[str], set[tuple[str, str]]]]],
    runtime_paths: RuntimePaths,
) -> list[AccountSpec]:
    specs: list[AccountSpec] = []
    for (route, is_custom), by_cred in sorted(payg_groups.items()):
        shared = get_api_key_for_provider(route, runtime_paths=runtime_paths)
        if isinstance(shared, str) and shared.strip():
            default_fp = _digest(shared.strip())
        elif route == "google" and (
            env_project := get_secret_from_env("GOOGLE_CLOUD_PROJECT", runtime_paths=runtime_paths)
        ):
            default_fp = _digest(f"vertex-project:{env_project.strip()}")
        else:
            default_fp = "__default__"
        ordered = sorted(by_cred.items(), key=lambda item: (item[0] != default_fp, item[1][0][0]))
        for idx, (_cred_fp, (keys, ukeys)) in enumerate(ordered):
            if is_custom:
                account_id = f"{route}:custom" if idx == 0 else f"{route}:custom:{idx + 1}"
            else:
                account_id = f"{route}:default" if idx == 0 else f"{route}:{idx + 1}"
            specs.append(
                AccountSpec(
                    id=account_id,
                    provider=route,
                    display_name=_display_name(route),
                    model_keys=tuple(keys),
                    usage_keys=frozenset(ukeys),
                    probe_kind="payg",
                ),
            )
    return specs


def discover_accounts(config: Config, runtime_paths: RuntimePaths) -> list[AccountSpec]:
    """Group configured models into distinct billing accounts with opaque external IDs."""
    env_codex = get_secret_from_env("CODEX_HOME", runtime_paths=runtime_paths)
    defaults = {"claude_code": _resolve_cli_scope(None)[1], "codex": normalize_codex_scope_key(env_codex)}
    grouped: dict[str, dict[str, tuple[str | None, list[str], set[tuple[str, str]]]]] = {
        k: {} for k in ("claude_code", "codex", "openrouter")
    }
    payg_groups: dict[tuple[str, bool], dict[str, tuple[list[str], set[tuple[str, str]]]]] = {}

    for model_key in sorted(config.models):
        model_config = config.models[model_key]
        route, identity, extra = (
            provider_route(model_config.provider),
            model_identity(model_config),
            model_config.extra_kwargs or {},
        )
        usage_key = (identity.provider, identity.id)
        if route == "codex":
            raw_home = extra.get("codex_home")
            target = raw_home.strip() if isinstance(raw_home, str) and raw_home.strip() else env_codex
            _, keys, ukeys = grouped["codex"].setdefault(normalize_codex_scope_key(target), (target, [], set()))
        elif route == "claude_code":
            raw_cli = extra.get("cli_path") if isinstance(extra.get("cli_path"), str) else None
            target, scope = _resolve_cli_scope(raw_cli)
            _, keys, ukeys = grouped["claude_code"].setdefault(scope, (target, [], set()))
        elif route in {"openrouter", "openai", "anthropic"} and _is_official_openrouter_url(
            _effective_base_url(route, model_config, runtime_paths),
        ):
            cred_kind, target, opaque_seed = _resolve_model_credential(model_key, model_config, runtime_paths)
            if cred_kind == "opaque":
                cred_fp = _digest(f"opaque:{opaque_seed}")
                keys, ukeys = payg_groups.setdefault(("openrouter", True), {}).setdefault(cred_fp, ([], set()))
            else:
                key_fp = _digest(target) if target else "__missing__"
                _, keys, ukeys = grouped["openrouter"].setdefault(key_fp, (target, [], set()))
        else:
            cred_fp = _payg_credential_fingerprint(model_key, model_config, runtime_paths)
            keys, ukeys = payg_groups.setdefault((route, route == "openrouter"), {}).setdefault(cred_fp, ([], set()))
        keys.append(model_key)
        ukeys.add(usage_key)

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
            scope_key=key_fp,
            target=api_key,
        )
        for i, (key_fp, (api_key, keys, ukeys)) in enumerate(
            sorted(grouped["openrouter"].items(), key=lambda item: item[1][1][0]),
            start=1,
        )
    )
    accounts.extend(_payg_specs(payg_groups, runtime_paths))
    return accounts
