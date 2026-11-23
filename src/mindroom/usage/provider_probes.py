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
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), Mapping) else {}
    windows: list[PassiveWindow] = []
    for slot, default_mins in (("primary", 300), ("secondary", 10080)):
        entry = rate_limit.get(f"{slot}_window")
        if not isinstance(entry, Mapping) or (used := finite_float(entry.get("used_percent"))) is None:
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


async def probe_codex(codex_home: str | None, now: datetime) -> ProbeResult:
    """Probe Codex ChatGPT usage windows via ``borrow_codex_key`` and ``wham/usage``."""
    try:
        token, account_id = await asyncio.to_thread(borrow_codex_key, codex_home=codex_home)
    except CodexAuthError:
        return ProbeResult(unavailable_reason="not_signed_in")
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
    data = fetched.get("data") if isinstance(fetched.get("data"), Mapping) else fetched
    limit = finite_float(data.get("limit"))
    if limit is None or limit <= 0:
        return ProbeResult(plan="Unlimited credit", account_label="Unlimited credit", observed_at=now, succeeded=True)
    rem_raw, usage_raw = finite_float(data.get("limit_remaining")), finite_float(data.get("usage"))
    if rem_raw is not None:
        remaining = max(0.0, rem_raw)
        used = max(0.0, limit - remaining)
    elif usage_raw is not None:
        used = max(0.0, usage_raw)
        remaining = max(0.0, limit - used)
    else:
        used, remaining = 0.0, limit
    reset_kind = str(data.get("limit_reset") or "").lower()
    kind: WindowKind = "session" if reset_kind == "daily" else ("weekly" if reset_kind == "weekly" else "monthly")
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


def _claude_percent(entry: Mapping[str, Any], *, scale: float) -> float | None:
    for key in ("used_percent", "used_percentage"):
        if (val := finite_float(entry.get(key))) is not None:
            return val
    if (util := finite_float(entry.get("utilization"))) is not None:
        return util * (100.0 if ("status" in entry and util <= 1.0) else scale)
    return None


def _collect_claude_entries(rate_limits: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    entries = [(b, e) for b in ("five_hour", "seven_day") if isinstance(e := rate_limits.get(b), Mapping)]
    scoped = rate_limits.get("model_scoped")
    if isinstance(scoped, Mapping):
        for key, entry in sorted(scoped.items()):
            if isinstance(entry, Mapping):
                entries.append((key if key.startswith("seven_day_") else f"seven_day_{key}", entry))
    elif isinstance(scoped, list):
        entries.extend(
            (k if k.startswith("seven_day_") else f"seven_day_{k}", entry)
            for entry in scoped
            if isinstance(entry, Mapping)
            and isinstance(k := (entry.get("id") or entry.get("model") or entry.get("name")), str)
        )
    seen = {b for b, _ in entries}
    for key, entry in sorted(rate_limits.items()):
        if isinstance(key, str) and key.startswith("seven_day_") and key not in seen and isinstance(entry, Mapping):
            entries.append((key, entry))
    return entries


def _parse_claude_usage(payload: Mapping[str, Any]) -> tuple[PassiveWindow, ...]:
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), Mapping) else payload
    entries = _collect_claude_entries(rate_limits)
    utils = [val for _, entry in entries if (val := finite_float(entry.get("utilization"))) is not None]
    scale = 100.0 if utils and max(utils) <= 1.0 and any(0.0 < val < 1.0 for val in utils) else 1.0
    windows = [
        build_window(
            window_id=wid,
            kind=kind,
            label=label,
            used_percent=used,
            window_duration_mins=duration,
            resets_at=parse_reset_timestamp(entry.get("resets_at") or entry.get("resetsAt")),
        )
        for bucket, entry in entries
        if (used := _claude_percent(entry, scale=scale)) is not None
        for wid, kind, label, duration in (claude_window_metadata(bucket),)
    ]
    extra = (
        rate_limits.get("extra_usage")
        if isinstance(rate_limits.get("extra_usage"), Mapping)
        else payload.get("extra_usage")
    )
    if isinstance(extra, Mapping) and extra.get("is_enabled", extra.get("enabled", True)) is not False:
        used_credits, monthly_limit = finite_float(extra.get("used_credits")), finite_float(extra.get("monthly_limit"))
        used_extra = (
            (used_credits / monthly_limit) * 100.0
            if used_credits is not None and monthly_limit and monthly_limit > 0
            else _claude_percent(extra, scale=scale)
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
    return sort_windows(windows)


async def _claude_control_rpc(proc: asyncio.subprocess.Process, request_id: str, subtype: str) -> dict[str, Any] | None:
    assert proc.stdin is not None
    assert proc.stdout is not None
    msg = json.dumps({"type": "control_request", "request_id": request_id, "request": {"subtype": subtype}}) + "\n"
    proc.stdin.write(msg.encode("utf-8"))
    await proc.stdin.drain()
    while raw_line := await proc.stdout.readline():
        try:
            frame = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(frame, Mapping) or frame.get("type") != "control_response":
            continue
        response = frame.get("response")
        if not isinstance(response, Mapping) or response.get("request_id") != request_id:
            continue
        if response.get("subtype") == "error":
            return None
        body = response.get("response")
        return dict(body) if isinstance(body, Mapping) else {}
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
                init_data = await _claude_control_rpc(proc, "mindroom-init", "initialize")
                if init_data is None:
                    return ProbeResult(unavailable_reason="probe_failed")
                acct = init_data.get("account") if isinstance(init_data.get("account"), Mapping) else {}
                other = acct.get("apiKeySource") or acct.get("tokenSource")
                if other == "none":
                    return ProbeResult(unavailable_reason="not_signed_in")
                if other not in (None, "claude.ai") or acct.get("apiProvider") != "firstParty":
                    return ProbeResult(unavailable_reason="unsupported")
                sub = str(acct.get("subscriptionType") or "")
                if not _CLAUDE_PLAN_RE.fullmatch(sub):
                    return ProbeResult(unavailable_reason="not_signed_in")
                plan = sub.removeprefix("Claude ").strip() or sub
                usage_data = await _claude_control_rpc(proc, "mindroom-usage", "get_usage")
                if usage_data is None:
                    return ProbeResult(unavailable_reason="probe_failed")
                return ProbeResult(
                    windows=_parse_claude_usage(usage_data),
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
