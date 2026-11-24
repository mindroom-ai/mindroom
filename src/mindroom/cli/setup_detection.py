"""Fast, read-only provider auto-detection, live API key checks, and inline CLI login for starter setup."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx

from mindroom import constants
from mindroom.credentials.environment import get_secret_from_env
from mindroom.model_defaults import (
    ANTHROPIC_BASE_URL_DEFAULT,
    OLLAMA_HOST_DEFAULT,
    OPENAI_BASE_URL_DEFAULT,
    OPENROUTER_BASE_URL_DEFAULT,
)
from mindroom.providers.claude_code.launcher import SCRUB_NAMES, SCRUB_PREFIXES
from mindroom.providers.key_checks import PROVIDER_KEY_CHECKS, provider_endpoint_variables
from mindroom.providers.openai.codex_auth import codex_login_status
from mindroom.providers.openai.kimi_auth import kimi_login_status

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

__all__ = [
    "DetectedProvider",
    "DetectionSnapshot",
    "KeyCheckResult",
    "KeyCheckStatus",
    "OllamaModelInfo",
    "detect_available_providers",
    "detect_single_provider",
    "inline_cli_login_command",
    "run_inline_cli_login",
    "verify_provider_api_key",
]

KeyCheckStatus = Literal["valid", "rejected", "inconclusive"]

_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS = 2.5
_OLLAMA_PROBE_TIMEOUT_SECONDS = 0.5
_API_KEY_CHECK_TIMEOUT_SECONDS = 5.0
_API_KEY_PRESETS = frozenset({"openai", "anthropic", "openrouter"})
_DETECTION_ORDER = ("claude_code", "codex", "kimi", "openai", "anthropic", "openrouter", "ollama")

_CLAUDE_SUBSCRIPTIONS = {
    "pro": "Claude Pro",
    "team": "Claude Team",
    "enterprise": "Claude Enterprise",
}
_CLAUDE_MAX_PLAN_RE = re.compile(r"^(?:claude\s+)?max(?:[\s_]+(\d+x))?$", re.IGNORECASE)
_EMBEDDING_MODEL_RE = re.compile(
    r"(?:embed|(?:^|[/._-])(?:bge-|all-minilm|e5-|paraphrase-multilingual))",
    re.IGNORECASE,
)
_CODEX_PLANS = {
    "pro": "ChatGPT Pro",
    "plus": "ChatGPT Plus",
    "team": "ChatGPT Team",
    "enterprise": "ChatGPT Enterprise",
    "free": "ChatGPT Free",
    "edu": "ChatGPT Edu",
}
_SUMMARY_LABELS = {
    "claude_code": "Claude",
    "codex": "Codex",
    "kimi": "Kimi Code",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "ollama": "Ollama",
}
_INLINE_LOGIN_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "codex": ("codex login", ("login",)),
    "claude_code": ("claude auth login", ("auth", "login")),
}
_STOCK_PROVIDER_BASE_URLS: dict[str, str] = {
    "anthropic": ANTHROPIC_BASE_URL_DEFAULT,
    "openai": OPENAI_BASE_URL_DEFAULT,
    "openrouter": OPENROUTER_BASE_URL_DEFAULT,
}


@dataclass(frozen=True, slots=True)
class OllamaModelInfo:
    """One pulled chat model reported by a reachable Ollama server."""

    name: str
    size_bytes: int | None = None

    def format_size(self) -> str | None:
        """Return a concise human-readable model size, or None when unavailable."""
        if self.size_bytes is None or self.size_bytes <= 0:
            return None
        if self.size_bytes >= 1_000_000_000:
            return f"{self.size_bytes / 1_000_000_000:.1f} GB"
        if self.size_bytes >= 1_000_000:
            return f"{self.size_bytes / 1_000_000:.0f} MB"
        return f"{self.size_bytes / 1_000:.0f} KB"


@dataclass(frozen=True, slots=True)
class DetectedProvider:
    """Probe outcome for one starter provider preset."""

    preset: str
    ready: bool
    status_detail: str
    cli_path: str | None = None
    ollama_models: tuple[OllamaModelInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class DetectionSnapshot:
    """Aggregated provider detection results for interactive setup."""

    providers: dict[str, DetectedProvider] = field(default_factory=dict)

    def get(self, preset: str) -> DetectedProvider | None:
        """Return the detected entry for ``preset``, if any."""
        return self.providers.get(preset)

    def both_claude_and_codex_ready(self) -> bool:
        """Return whether both Claude subscription and Codex ChatGPT logins are ready."""
        claude, codex = self.providers.get("claude_code"), self.providers.get("codex")
        return bool(claude and claude.ready and codex and codex.ready)

    def summary_line(self) -> str | None:
        """Return a compact one-line summary of detected providers, or None when nothing was found."""
        if not self.providers:
            return None
        ordered = sorted(self.providers.values(), key=lambda info: not info.ready)
        parts = [f"{_SUMMARY_LABELS.get(info.preset, info.preset)} ({info.status_detail})" for info in ordered]
        return f"Detected: {', '.join(parts)}"


@dataclass(frozen=True, slots=True)
class KeyCheckResult:
    """Outcome of verifying an API key against a provider's read-only check endpoint."""

    status: KeyCheckStatus
    detail: str = ""
    custom_endpoint: bool = False


def detect_available_providers(runtime_paths: RuntimePaths, *, timeout_seconds: float = 3.0) -> DetectionSnapshot:
    """Probe local logins, environment API keys, and local Ollama in parallel; always fails open."""
    probe_timeout = max(0.05, timeout_seconds)
    executor = ThreadPoolExecutor(max_workers=len(_DETECTION_ORDER))
    futures: dict[Future[DetectedProvider | None], str] = {
        executor.submit(_probe_preset, preset, runtime_paths, probe_timeout): preset for preset in _DETECTION_ORDER
    }
    done, not_done = wait(futures, timeout=probe_timeout)
    executor.shutdown(wait=not not_done, cancel_futures=bool(not_done))

    raw: dict[str, DetectedProvider] = {}
    for future in done:
        with suppress(Exception):
            if (detected := future.result()) is not None:
                raw[futures[future]] = detected
    return DetectionSnapshot(providers={preset: raw[preset] for preset in _DETECTION_ORDER if preset in raw})


def detect_single_provider(
    preset: str,
    runtime_paths: RuntimePaths,
    *,
    timeout_seconds: float = 3.0,
) -> DetectedProvider | None:
    """Run the availability probe for a single preset; fails open to None on error."""
    with suppress(Exception):
        return _probe_preset(preset, runtime_paths, max(0.05, timeout_seconds))
    return None


def verify_provider_api_key(
    preset: str,
    api_key: str,
    *,
    runtime_paths: RuntimePaths | None = None,
    timeout_seconds: float = _API_KEY_CHECK_TIMEOUT_SECONDS,
) -> KeyCheckResult:
    """Verify an API key using the provider's cheap read-only endpoint without logging or echoing the key."""
    label = _SUMMARY_LABELS.get(preset, preset)
    if not api_key.isascii() or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in api_key):
        return KeyCheckResult(status="rejected", detail=f"{label} API key contains invalid characters.")
    if (
        runtime_paths is not None
        and (env_key := constants.env_key_for_provider(preset))
        and (endpoint_vars := _custom_endpoint_variables(preset, env_key, runtime_paths))
    ):
        return KeyCheckResult(
            status="inconclusive",
            detail=f"Custom endpoint configured ({', '.join(endpoint_vars)}); skipping {label} key check.",
            custom_endpoint=True,
        )
    check = PROVIDER_KEY_CHECKS.get(preset)
    if check is None:
        return KeyCheckResult(status="inconclusive", detail="No verification endpoint for this provider.")
    url, headers = check.request(api_key)
    try:
        response = httpx.get(url, headers=headers, timeout=timeout_seconds, follow_redirects=False)
    except (httpx.HTTPError, ImportError, OSError):
        return KeyCheckResult(status="inconclusive", detail="Could not reach provider.")
    if 200 <= response.status_code < 300:
        return KeyCheckResult(status="valid")
    status: KeyCheckStatus = "rejected" if response.status_code == 401 else "inconclusive"
    return KeyCheckResult(status=status, detail=f"HTTP {response.status_code}")


def inline_cli_login_command(preset: str) -> str | None:
    """Return the human-readable CLI login command for presets that support inline login."""
    spec = _INLINE_LOGIN_SPECS.get(preset)
    return spec[0] if spec else None


def run_inline_cli_login(preset: str, cli_path: str, *, runtime_paths: RuntimePaths | None = None) -> bool:
    """Run the provider CLI's interactive login attached to the current terminal; return whether it exited 0."""
    spec = _INLINE_LOGIN_SPECS.get(preset)
    if spec is None:
        return False
    env: dict[str, str] | None = None
    if preset == "claude_code":
        env = _scrubbed_claude_env()
    elif preset == "codex" and runtime_paths is not None and (codex_home := _resolved_codex_home(runtime_paths)):
        env = {**os.environ, "CODEX_HOME": codex_home}
    try:
        return subprocess.run([cli_path, *spec[1]], env=env, check=False).returncode == 0
    except OSError:
        return False


def _probe_preset(preset: str, runtime_paths: RuntimePaths, timeout_seconds: float) -> DetectedProvider | None:
    if preset == "claude_code":
        return _probe_claude_code(timeout_seconds=timeout_seconds)
    if preset == "codex":
        return _probe_codex(runtime_paths)
    if preset == "kimi":
        return _probe_kimi(runtime_paths)
    if preset in _API_KEY_PRESETS:
        env_key = constants.env_key_for_provider(preset)
        return (
            DetectedProvider(preset, True, f"{env_key} set")
            if env_key and get_secret_from_env(env_key, runtime_paths)
            else None
        )
    if preset == "ollama":
        return _probe_ollama(runtime_paths, timeout_seconds=timeout_seconds)
    return None


def _resolve_cli_path(executable: str) -> str | None:
    found = shutil.which(executable)
    return str(Path(found).absolute()) if found else None


def _scrubbed_claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith(SCRUB_PREFIXES) and k not in SCRUB_NAMES}


def _format_codex_plan(plan_type: str | None) -> str:
    cleaned = (plan_type or "").strip()
    if not cleaned:
        return "signed in"
    lowered = cleaned.lower()
    return cleaned if lowered.startswith("chatgpt") else _CODEX_PLANS.get(lowered, f"ChatGPT {cleaned.title()}")


def _format_claude_subscription(raw_subscription: object) -> str | None:
    """Return a clean label only for paid Claude subscription plans, or None for free/unsupported plans."""
    if not isinstance(raw_subscription, str) or not (cleaned := raw_subscription.strip()):
        return None
    normalized = cleaned.lower().removeprefix("claude ").strip()
    if normalized in _CLAUDE_SUBSCRIPTIONS:
        return _CLAUDE_SUBSCRIPTIONS[normalized]
    if match := _CLAUDE_MAX_PLAN_RE.fullmatch(cleaned):
        return f"Claude Max {match.group(1).lower()}" if match.group(1) else "Claude Max"
    return None


def _custom_endpoint_variables(preset: str, env_key: str, runtime_paths: RuntimePaths) -> list[str]:
    stock_url = _STOCK_PROVIDER_BASE_URLS.get(preset)
    normalized_stock = stock_url.rstrip("/") if stock_url is not None else None
    return [
        name
        for name in provider_endpoint_variables(env_key, runtime_paths)
        if normalized_stock is None
        or (value := get_secret_from_env(name, runtime_paths)) is None
        or value.strip().rstrip("/") != normalized_stock
    ]


def _resolved_codex_home(runtime_paths: RuntimePaths) -> str | None:
    raw = get_secret_from_env("CODEX_HOME", runtime_paths)
    if not raw or not (cleaned := raw.strip()):
        return None
    return str(Path(cleaned).expanduser())


def _probe_codex(runtime_paths: RuntimePaths) -> DetectedProvider | None:
    codex_home = _resolved_codex_home(runtime_paths)
    cli_path = _resolve_cli_path("codex")
    status = codex_login_status(codex_home=codex_home)
    if status.signed_in:
        return DetectedProvider("codex", True, _format_codex_plan(status.plan_type), cli_path=cli_path)
    return DetectedProvider("codex", False, "CLI installed", cli_path=cli_path) if cli_path else None


def _probe_claude_code(*, timeout_seconds: float) -> DetectedProvider | None:
    # Compliance: never read ~/.claude* or OS keychains; ask the Claude CLI itself with scrubbed env.
    cli_path = _resolve_cli_path("claude")
    if not cli_path:
        return None
    installed_only = DetectedProvider("claude_code", False, "CLI installed", cli_path=cli_path)
    try:
        proc = subprocess.run(
            [cli_path, "auth", "status"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=_scrubbed_claude_env(),
            timeout=min(_CLAUDE_AUTH_STATUS_TIMEOUT_SECONDS, timeout_seconds),
            check=False,
        )
        payload = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return installed_only
    if (
        isinstance(payload, dict)
        and payload.get("loggedIn") is True
        and payload.get("authMethod") == "claude.ai"
        and payload.get("apiProvider") == "firstParty"
        and (detail := _format_claude_subscription(payload.get("subscriptionType"))) is not None
    ):
        return DetectedProvider("claude_code", True, detail, cli_path=cli_path)
    return installed_only


def _probe_kimi(runtime_paths: RuntimePaths) -> DetectedProvider | None:
    kimi_home = (runtime_paths.env_value("KIMI_CODE_HOME") or "").strip() or None
    cli_path = _resolve_cli_path("kimi")
    if kimi_login_status(kimi_home=kimi_home):
        return DetectedProvider("kimi", True, "signed in", cli_path=cli_path)
    return DetectedProvider("kimi", False, "CLI installed", cli_path=cli_path) if cli_path else None


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_ollama_host(raw_host: str) -> tuple[str, str] | None:
    """Normalize an Ollama host matching Ollama SDK defaults (`http://` and default port when omitted)."""
    if raw_host.startswith(":"):
        raw_host = f"127.0.0.1{raw_host}"
    default_port = urlsplit(OLLAMA_HOST_DEFAULT).port or 11434
    scheme, sep, hostport = raw_host.partition("://")
    if not sep:
        scheme, hostport, port = "http", raw_host, default_port
    elif scheme == "http":
        port = 80
    elif scheme == "https":
        port = 443
    else:
        return None
    try:
        parsed = urlsplit(f"{scheme}://{hostport}")
        resolved_port = parsed.port or port
    except ValueError:
        return None
    if not (hostname := parsed.hostname):
        return None
    host_literal = f"[{hostname}]" if ":" in hostname else hostname
    path = parsed.path.strip("/")
    base = f"{scheme}://{host_literal}:{resolved_port}"
    return (f"{base}/{path}" if path else base), hostname


def _parse_ollama_models(payload: object) -> tuple[tuple[OllamaModelInfo, ...], int] | None:
    """Return `(chat_models, total_model_count)` from `/api/tags`, excluding obvious embedding-only models."""
    if not isinstance(payload, dict) or not isinstance(raw_models := payload.get("models"), list):
        return None
    chat_models: list[OllamaModelInfo] = []
    seen: set[str] = set()
    total_count = 0
    for entry in raw_models:
        if not isinstance(entry, dict) or not isinstance(raw_name := entry.get("name"), str):
            continue
        if not (name := raw_name.strip()) or name in seen:
            continue
        seen.add(name)
        total_count += 1
        if _EMBEDDING_MODEL_RE.search(name):
            continue
        raw_size = entry.get("size")
        size_bytes = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else None
        chat_models.append(OllamaModelInfo(name=name, size_bytes=size_bytes))
    return tuple(chat_models), total_count


def _probe_ollama(runtime_paths: RuntimePaths, *, timeout_seconds: float) -> DetectedProvider | None:
    configured_host = (runtime_paths.env_value("OLLAMA_HOST") or "").strip()
    normalized = _normalize_ollama_host(configured_host or OLLAMA_HOST_DEFAULT)
    if normalized is None:
        return None
    host, hostname = normalized
    is_loopback = _is_loopback_host(hostname)
    if not configured_host and not is_loopback:
        return None
    try:
        with httpx.Client(
            timeout=min(_OLLAMA_PROBE_TIMEOUT_SECONDS, timeout_seconds),
            follow_redirects=False,
            trust_env=not is_loopback,
        ) as client:
            response = client.get(f"{host}/api/tags")
            payload = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ImportError, OSError, ValueError):
        return None
    if (parsed := _parse_ollama_models(payload)) is None:
        return None
    chat_models, total_count = parsed
    if not chat_models:
        detail = "running (no chat models pulled)" if total_count > 0 else "running (no models pulled)"
        return DetectedProvider("ollama", False, detail)
    detail = (
        ", ".join(model.name for model in chat_models)
        if len(chat_models) <= 3
        else f"{chat_models[0].name}, {chat_models[1].name}, +{len(chat_models) - 2} more"
    )
    return DetectedProvider("ollama", True, detail, ollama_models=chat_models)
