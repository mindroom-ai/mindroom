"""On-demand rate-limit and credit probes for Codex, Claude Code, and OpenRouter."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx

from mindroom.model_defaults import OPENROUTER_BASE_URL_DEFAULT
from mindroom.providers.claude_code import launcher as claude_launcher
from mindroom.providers.openai.codex_auth import CodexAuthError, borrow_codex_key
from mindroom.usage.limit_store import (
    PassiveWindow,
    WindowKind,
    build_window,
    claude_window_metadata,
    codex_window_metadata,
    finite_float,
    format_utc_iso,
    parse_reset_timestamp,
    sort_windows,
)

__all__ = [
    "CLAUDE_PROBE_TIMEOUT_SECONDS",
    "CODEX_USAGE_URL",
    "HTTP_PROBE_TIMEOUT_SECONDS",
    "OPENROUTER_KEY_URL",
    "ProbeResult",
    "UnavailableReason",
    "probe_claude_code",
    "probe_codex",
    "probe_openrouter",
]

UnavailableReason = Literal["unsupported", "not_signed_in", "probe_failed"]

CLAUDE_PROBE_TIMEOUT_SECONDS = 10.0
HTTP_PROBE_TIMEOUT_SECONDS = 10.0
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
OPENROUTER_KEY_URL = f"{OPENROUTER_BASE_URL_DEFAULT}/key"

_CLAUDE_PLAN_RE = re.compile(r"Claude (Pro|Team|Enterprise|Max( \d+x)?)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CODEX_PLANS = {
    "pro": "ChatGPT Pro",
    "plus": "ChatGPT Plus",
    "team": "ChatGPT Team",
    "enterprise": "ChatGPT Enterprise",
    "free": "ChatGPT Free",
    "edu": "ChatGPT Edu",
}
_CLAUDE_ISOLATION_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_CRON": "1",
    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1",
}
_CLAUDE_CLI_FLAGS = (
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--tools",
    "",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--permission-mode",
    "dontAsk",
    "--settings",
    '{"disableAllHooks":true}',
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of probing one provider account."""

    windows: tuple[PassiveWindow, ...] = ()
    plan: str | None = None
    account_label: str | None = None
    unavailable_reason: UnavailableReason | None = None
    observed_at: datetime | None = None
    succeeded: bool = False


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


async def _fetch_json_or_error(url: str, headers: dict[str, str]) -> dict[str, Any] | UnavailableReason:
    try:
        async with httpx.AsyncClient(timeout=HTTP_PROBE_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except Exception:
        return "probe_failed"
    if response.status_code in {401, 403}:
        return "not_signed_in"
    if response.status_code != 200:
        return "probe_failed"
    try:
        data = response.json()
    except ValueError:
        return "probe_failed"
    return data if isinstance(data, dict) else "probe_failed"


def _parse_codex_payload(data: Mapping[str, Any], now: datetime) -> ProbeResult:
    raw_plan = data.get("plan_type")
    plan = (
        _CODEX_PLANS.get(raw_plan.strip().lower(), raw_plan.strip())
        if isinstance(raw_plan, str) and raw_plan.strip()
        else None
    )
    rate_limit = _as_mapping(data.get("rate_limit")) or {}
    windows: list[PassiveWindow] = []
    for slot, default_mins in (("primary", 300), ("secondary", 10080)):
        entry = _as_mapping(rate_limit.get(f"{slot}_window"))
        if entry is None or (used := finite_float(entry.get("used_percent"))) is None:
            continue
        secs, mins = finite_float(entry.get("limit_window_seconds")), finite_float(entry.get("window_minutes"))
        duration = round(secs / 60.0) if secs and secs > 0 else (round(mins) if mins and mins > 0 else default_mins)
        resets_at = parse_reset_timestamp(entry.get("reset_at") or entry.get("resets_at"))
        if resets_at is None and (after := finite_float(entry.get("reset_after_seconds"))) and after > 0:
            resets_at = format_utc_iso(now + timedelta(seconds=after))
        window_id, kind, label = codex_window_metadata(slot, duration)
        windows.append(
            build_window(
                window_id=window_id,
                kind=kind,
                label=label,
                used_percent=used,
                window_duration_mins=duration,
                resets_at=resets_at,
            ),
        )
    ok = bool(windows or plan)
    return ProbeResult(
        windows=sort_windows(windows),
        plan=plan,
        account_label=plan,
        observed_at=now,
        succeeded=ok,
        unavailable_reason=None if ok else "probe_failed",
    )


def _is_transient_codex_auth_error(exc: CodexAuthError) -> bool:
    """Return whether ``exc`` came from a transient refresh/transport failure rather than missing/revoked login."""
    if isinstance(exc.__cause__, (httpx.HTTPError, json.JSONDecodeError, TimeoutError, ConnectionError)):
        return True
    if exc.status_code is not None:
        return exc.status_code in {408, 429} or exc.status_code >= 500
    message = str(exc).lower()
    return "token refresh returned" in message or "token refresh response" in message


async def probe_codex(codex_home: str | None, now: datetime) -> ProbeResult:
    """Probe Codex ChatGPT usage windows via ``borrow_codex_key`` and ``wham/usage``."""
    try:
        token, account_id = await asyncio.to_thread(borrow_codex_key, codex_home=codex_home)
    except CodexAuthError as exc:
        reason: UnavailableReason = "probe_failed" if _is_transient_codex_auth_error(exc) else "not_signed_in"
        return ProbeResult(unavailable_reason=reason)
    except Exception:
        return ProbeResult(unavailable_reason="probe_failed")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    fetched = await _fetch_json_or_error(CODEX_USAGE_URL, headers)
    if isinstance(fetched, str):
        return ProbeResult(unavailable_reason=fetched)
    return _parse_codex_payload(fetched, now)


async def probe_openrouter(api_key: str | None, now: datetime) -> ProbeResult:
    """Probe OpenRouter key credit cap via ``GET https://openrouter.ai/api/v1/key``."""
    if not api_key:
        return ProbeResult(unavailable_reason="not_signed_in")
    fetched = await _fetch_json_or_error(
        OPENROUTER_KEY_URL,
        {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    if isinstance(fetched, str):
        return ProbeResult(unavailable_reason=fetched)
    data = _as_mapping(fetched.get("data")) or fetched
    raw_limit = data.get("limit")
    if raw_limit is None:
        return ProbeResult(plan="Unlimited credit", account_label="Unlimited credit", observed_at=now, succeeded=True)
    limit = finite_float(raw_limit)
    if limit is None:
        return ProbeResult(unavailable_reason="probe_failed")
    reset_kind = str(data.get("limit_reset") or "").lower()
    kind: WindowKind = "session" if reset_kind == "daily" else ("weekly" if reset_kind == "weekly" else "monthly")
    if limit <= 0:
        window = build_window(
            window_id="credit_limit",
            kind=kind,
            label="Credit limit",
            used_percent=100.0,
            detail="$0.00 / $0.00 left",
        )
        return ProbeResult(
            windows=(window,),
            plan="Credit cap",
            account_label="Credit cap",
            observed_at=now,
            succeeded=True,
        )
    rem_raw, usage_raw = finite_float(data.get("limit_remaining")), finite_float(data.get("usage"))
    if rem_raw is not None:
        remaining = max(0.0, rem_raw)
        used = max(0.0, limit - remaining)
    elif usage_raw is not None:
        used = max(0.0, usage_raw)
        remaining = max(0.0, limit - used)
    else:
        used, remaining = 0.0, limit
    window = build_window(
        window_id="credit_limit",
        kind=kind,
        label="Credit limit",
        used_percent=(used / limit) * 100.0,
        detail=f"${remaining:.2f} / ${limit:.2f} left",
    )
    return ProbeResult(
        windows=(window,),
        plan="Credit cap",
        account_label="Credit cap",
        observed_at=now,
        succeeded=True,
    )


def _claude_percent(entry: Mapping[str, Any]) -> float | None:
    """Read an active Claude ``get_usage`` percentage (always 0..100)."""
    for key in ("used_percent", "used_percentage", "utilization"):
        if (val := finite_float(entry.get(key))) is not None:
            return val
    return None


def _model_scoped_bucket(raw_name: str) -> str:
    slug = _SLUG_RE.sub("_", raw_name.strip().lower()).strip("_") or "model"
    return slug if slug.startswith("seven_day_") else f"seven_day_{slug}"


def _model_scoped_label(entry: Mapping[str, Any], default_label: str) -> str:
    display_name = entry.get("display_name")
    if not isinstance(display_name, str) or not (cleaned := display_name.strip()):
        return default_label
    lowered = cleaned.lower()
    if any(token in lowered for token in ("7-day", "7 day", "weekly", "5-hour", "5 hour")):
        return cleaned
    return f"7-day {cleaned}"


def _model_scoped_entries(scoped: object) -> list[tuple[str, Mapping[str, Any]]]:
    if isinstance(scoped, Mapping):
        return [
            (_model_scoped_bucket(key), entry)
            for key, raw_entry in sorted(scoped.items())
            if isinstance(key, str) and (entry := _as_mapping(raw_entry)) is not None
        ]
    if not isinstance(scoped, list):
        return []
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for raw_entry in scoped:
        if (entry := _as_mapping(raw_entry)) is None:
            continue
        raw_name = (
            entry.get("id")
            or entry.get("rate_limit_type")
            or entry.get("model")
            or entry.get("name")
            or entry.get("display_name")
        )
        if isinstance(raw_name, str) and raw_name.strip():
            entries.append((_model_scoped_bucket(raw_name), entry))
    return entries


def _collect_claude_entries(rate_limits: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    entries = [
        (bucket, entry)
        for bucket in ("five_hour", "seven_day")
        if (entry := _as_mapping(rate_limits.get(bucket))) is not None
    ]
    entries.extend(_model_scoped_entries(rate_limits.get("model_scoped")))
    seen = {bucket for bucket, _ in entries}
    entries.extend(
        (key, entry)
        for key, raw_entry in sorted(rate_limits.items())
        if isinstance(key, str)
        and key.startswith("seven_day_")
        and key not in seen
        and (entry := _as_mapping(raw_entry)) is not None
    )
    return entries


def _parse_claude_usage(
    payload: Mapping[str, Any],
) -> tuple[PassiveWindow, ...] | UnavailableReason:
    if payload.get("rate_limits_available") is False:
        return "unsupported"
    if "rate_limits" in payload:
        rate_limits = _as_mapping(payload.get("rate_limits"))
        if rate_limits is None:
            return "probe_failed"
    elif _as_mapping(payload.get("five_hour")) is not None or _as_mapping(payload.get("seven_day")) is not None:
        rate_limits = payload
    else:
        return "probe_failed"

    entries = _collect_claude_entries(rate_limits)
    windows: list[PassiveWindow] = []
    for bucket, entry in entries:
        if (used := _claude_percent(entry)) is None:
            continue
        wid, kind, default_label, duration = claude_window_metadata(bucket)
        label = _model_scoped_label(entry, default_label) if bucket.startswith("seven_day_") else default_label
        windows.append(
            build_window(
                window_id=wid,
                kind=kind,
                label=label,
                used_percent=used,
                window_duration_mins=duration,
                resets_at=parse_reset_timestamp(entry.get("resets_at") or entry.get("resetsAt")),
            ),
        )
    extra = _as_mapping(rate_limits.get("extra_usage")) or _as_mapping(payload.get("extra_usage"))
    if extra is not None and extra.get("is_enabled", extra.get("enabled", True)) is not False:
        used_credits, monthly_limit = finite_float(extra.get("used_credits")), finite_float(extra.get("monthly_limit"))
        used_extra = (
            (used_credits / monthly_limit) * 100.0
            if used_credits is not None and monthly_limit and monthly_limit > 0
            else _claude_percent(extra)
        )
        if used_extra is not None:
            detail = extra.get("detail") if isinstance(extra.get("detail"), str) else None
            windows.append(
                build_window(
                    window_id="extra_usage",
                    kind="monthly",
                    label="Extra usage",
                    used_percent=used_extra,
                    resets_at=parse_reset_timestamp(extra.get("resets_at") or extra.get("resetsAt")),
                    detail=detail,
                ),
            )
    if not windows:
        return "probe_failed"
    return sort_windows(windows)


async def _claude_control_rpc(
    proc: asyncio.subprocess.Process,
    request_id: str,
    request: dict[str, object],
) -> dict[str, Any] | None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    msg = json.dumps({"type": "control_request", "request_id": request_id, "request": request}) + "\n"
    proc.stdin.write(msg.encode("utf-8"))
    await proc.stdin.drain()
    while raw_line := await proc.stdout.readline():
        try:
            frame = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(frame, Mapping) or frame.get("type") != "control_response":
            continue
        response = _as_mapping(frame.get("response"))
        if response is None or response.get("request_id") != request_id:
            continue
        if response.get("subtype") == "error":
            return None
        body = _as_mapping(response.get("response"))
        return dict(body) if body is not None else {}
    return None


async def _reap_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None and not proc.stdin.is_closing():
        with suppress(OSError):
            proc.stdin.close()
    for action, wait_secs in ((None, 0.3), (proc.terminate, 0.5), (proc.kill, 2.0)):
        if proc.returncode is not None:
            return
        if action is not None:
            with suppress(ProcessLookupError):
                action()
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=wait_secs)


async def probe_claude_code(cli_path: str | None, now: datetime) -> ProbeResult:  # noqa: PLR0911
    """Probe Claude Code usage via an isolated ``launcher.py`` stream-json control session."""
    if not cli_path or not Path(cli_path).is_file():
        return ProbeResult(unavailable_reason="not_signed_in")
    keep = {k for k in _CLAUDE_ISOLATION_ENV if k.startswith(claude_launcher.SCRUB_PREFIXES)}
    env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith(claude_launcher.SCRUB_PREFIXES) or k in claude_launcher.SCRUB_NAMES)
    }
    env |= {
        **_CLAUDE_ISOLATION_ENV,
        claude_launcher.CLI_ENV: cli_path,
        claude_launcher.KEEP_ENV: " ".join(sorted(keep)),
    }
    proc: asyncio.subprocess.Process | None = None
    with tempfile.TemporaryDirectory(prefix="mindroom-claude-usage-") as tmpdir:
        Path(tmpdir).chmod(0o700)
        try:
            async with asyncio.timeout(CLAUDE_PROBE_TIMEOUT_SECONDS):
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-IS",
                    str(Path(claude_launcher.__file__).resolve()),
                    *_CLAUDE_CLI_FLAGS,
                    cwd=tmpdir,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                init_data = await _claude_control_rpc(proc, "mindroom-init", {"subtype": "initialize"})
                if init_data is None:
                    return ProbeResult(unavailable_reason="probe_failed")
                acct = _as_mapping(init_data.get("account")) or {}
                other = acct.get("apiKeySource") or acct.get("tokenSource")
                if other == "none":
                    return ProbeResult(unavailable_reason="not_signed_in")
                if other not in (None, "claude.ai") or acct.get("apiProvider") != "firstParty":
                    return ProbeResult(unavailable_reason="unsupported")
                sub = str(acct.get("subscriptionType") or "")
                if not _CLAUDE_PLAN_RE.fullmatch(sub):
                    return ProbeResult(unavailable_reason="not_signed_in")
                plan = sub.removeprefix("Claude ").strip() or sub
                usage_data = await _claude_control_rpc(
                    proc,
                    "mindroom-usage",
                    {"subtype": "get_usage", "skip_behaviors": True},
                )
                if usage_data is None:
                    return ProbeResult(unavailable_reason="probe_failed")
                parsed = _parse_claude_usage(usage_data)
                if isinstance(parsed, str):
                    return ProbeResult(unavailable_reason=parsed)
                return ProbeResult(
                    windows=parsed,
                    plan=plan,
                    account_label=plan,
                    observed_at=now,
                    succeeded=True,
                )
        except Exception:
            return ProbeResult(unavailable_reason="probe_failed")
        finally:
            if proc is not None:
                await _reap_process(proc)
