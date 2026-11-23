"""Tests for passive limit capture, provider probes, 7-day attribution, caching, and budgeting."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest
from mindroom.llm.usage import TokenCounts
from mindroom.providers.claude_code.bridge import ToolBridge
from mindroom.providers.claude_code.session import LiveSession
from mindroom.providers.claude_code.turn import _Step
from mindroom.providers.openai.adapter_common import SDKClients
from mindroom.providers.openai.codex_auth import CodexAuthError
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import ResponsesWire
from mindroom.providers.openai.settings import ResponsesSettings
from mindroom.usage import limit_store, provider_limits, provider_probes
from mindroom.usage.limit_store import PassiveWindow, build_window, normalize_codex_scope_key
from mindroom.usage.provider_limits import (
    DailyAccountUsage,
    ProviderLimitAccount,
    ProviderUsageSnapshot,
    fit_to_device_budget,
    get_provider_usage_snapshot,
)
from mindroom.usage.stats import (
    UsageCoverage,
    UsageDailyBreakdownRow,
    UsageModelBreakdownRow,
    UsageReport,
)
from tests.conftest import test_runtime_paths

CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "provider_usage_contract.json"
FIXED_NOW = datetime(2026, 11, 23, 20, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_stores() -> None:
    limit_store.clear()
    provider_limits.clear_cache()


def _empty_report(daily: tuple[UsageDailyBreakdownRow, ...] = ()) -> UsageReport:
    cov = UsageCoverage(scanned_sources=1, unavailable_sources=0)
    return UsageReport(
        scope="admin",
        totals=TokenCounts(),
        session_count=0,
        breakdown=(),
        coverage=cov,
        model_breakdown=(),
        model_coverage=cov,
        cumulative_model_breakdown=(),
        cumulative_model_coverage=cov,
        user_breakdown=(),
        daily_breakdown=daily,
        daily_coverage=cov,
    )


def _mock_httpx(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:  # noqa: ANN401
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _patched(*args: Any, **kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(provider_probes.httpx, "AsyncClient", _patched)


def _make_fake_claude_cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_wire_contract_fixture_round_trip() -> None:
    """ProviderUsageSnapshot.to_dict matches the locked wire contract fixture verbatim."""
    expected = json.loads(CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    snapshot = ProviderUsageSnapshot(
        checked_at="2026-11-23T20:00:00Z",
        partial=False,
        accounts=(
            ProviderLimitAccount(
                id="claude_code:default",
                provider="claude_code",
                display_name="Claude Code",
                account_label="Max 20x",
                plan="Max 20x",
                model_keys=("opus_native", "sonnet_native"),
                windows=(
                    build_window(
                        window_id="five_hour",
                        kind="session",
                        label="5-hour window",
                        used_percent=38.0,
                        window_duration_mins=300,
                        resets_at="2026-11-23T22:24:00Z",
                    ),
                    build_window(
                        window_id="seven_day",
                        kind="weekly",
                        label="7-day window",
                        used_percent=22.0,
                        window_duration_mins=10080,
                        resets_at="2026-11-26T09:00:00Z",
                    ),
                ),
                unavailable_reason=None,
                checked_at="2026-11-23T20:00:00Z",
                stale=False,
                tokens_7d=14200000,
                cost_usd_7d=None,
                cost_estimated=False,
            ),
            ProviderLimitAccount(
                id="codex:default",
                provider="codex",
                display_name="Codex",
                account_label="ChatGPT Pro",
                plan="ChatGPT Pro",
                model_keys=("astra", "sol"),
                windows=(
                    build_window(
                        window_id="five_hour",
                        kind="session",
                        label="5-hour window",
                        used_percent=88.0,
                        window_duration_mins=300,
                        resets_at="2026-11-23T23:06:00Z",
                    ),
                    build_window(
                        window_id="seven_day",
                        kind="weekly",
                        label="7-day window",
                        used_percent=81.0,
                        window_duration_mins=10080,
                        resets_at="2026-11-25T14:00:00Z",
                    ),
                ),
                unavailable_reason=None,
                checked_at="2026-11-23T20:00:00Z",
                stale=False,
                tokens_7d=9800000,
                cost_usd_7d=None,
                cost_estimated=False,
            ),
            ProviderLimitAccount(
                id="openrouter:1",
                provider="openrouter",
                display_name="OpenRouter",
                account_label="Credit cap",
                plan="Credit cap",
                model_keys=("neon",),
                windows=(
                    build_window(
                        window_id="credit_limit",
                        kind="monthly",
                        label="Credit limit",
                        used_percent=36.8,
                        detail="$31.60 / $50.00 left",
                    ),
                ),
                unavailable_reason=None,
                checked_at="2026-11-23T20:00:00Z",
                stale=False,
                tokens_7d=6400000,
                cost_usd_7d=18.4,
                cost_estimated=True,
            ),
            ProviderLimitAccount(
                id="anthropic:default",
                provider="anthropic",
                display_name="Anthropic",
                account_label="Pay-as-you-go",
                plan="Pay-as-you-go",
                model_keys=("haiku",),
                windows=(),
                unavailable_reason=None,
                checked_at="2026-11-23T20:00:00Z",
                stale=False,
                tokens_7d=1200000,
                cost_usd_7d=3.25,
                cost_estimated=True,
            ),
        ),
        daily_7d=(
            DailyAccountUsage(
                day="2026-11-17",
                by_account={"claude_code:default": 1900000, "codex:default": 1200000, "openrouter:1": 900000},
                unattributed_tokens=0,
            ),
            DailyAccountUsage(
                day="2026-11-18",
                by_account={"claude_code:default": 2100000, "codex:default": 1500000, "openrouter:1": 850000},
                unattributed_tokens=0,
            ),
        ),
        unattributed_tokens_7d=0,
        unattributed_cost_usd_7d=None,
    )
    assert snapshot.to_dict(request_id="req-provider-usage-1") == expected
    rest_payload = snapshot.to_dict()
    assert "request_id" not in rest_payload


def test_limit_store_codex_and_claude_parsing() -> None:
    """Passive store parses case-insensitive Codex headers, Claude buckets, and ignores NaN/garbage."""
    limit_store.record_codex_headers(
        "scope-codex-a",
        {
            "X-Codex-Primary-Used-Percent": "42.5",
            "X-Codex-Primary-Window-Minutes": "300",
            "X-Codex-Primary-Reset-At": "1795475160",
            "x-codex-secondary-used-percent": "NaN",
        },
        now=FIXED_NOW,
    )
    reading = limit_store.get_passive_windows("codex", "scope-codex-a")
    assert reading is not None
    assert len(reading.windows) == 1
    assert reading.windows[0] == PassiveWindow(
        id="five_hour",
        kind="session",
        label="5-hour window",
        used_percent=42.5,
        remaining_percent=57.5,
        window_duration_mins=300,
        resets_at="2026-11-23T23:06:00Z",
    )

    limit_store.record_claude_rate_limit(
        "/bin/claude",
        rate_limit_type="five_hour",
        utilization=0.38,
        resets_at=1795472640,
        now=FIXED_NOW,
    )
    limit_store.record_claude_rate_limit(
        "/bin/claude",
        rate_limit_type="seven_day_sonnet",
        utilization=0.15,
        resets_at=1795615200,
        now=FIXED_NOW,
    )
    limit_store.record_claude_rate_limit(
        "/bin/claude",
        rate_limit_type="seven_day",
        utilization=float("nan"),
        resets_at=None,
        now=FIXED_NOW,
    )
    claude_reading = limit_store.get_passive_windows("claude_code", "/bin/claude")
    assert claude_reading is not None
    assert [(w.id, w.label, w.used_percent, w.window_duration_mins) for w in claude_reading.windows] == [
        ("five_hour", "5-hour window", 38.0, 300),
        ("seven_day_sonnet", "7-day Sonnet", 15.0, 10080),
    ]


@pytest.mark.asyncio
async def test_passive_hooks_codex_stream_and_claude_active_and_parked(tmp_path: Path) -> None:
    """Codex Responses stream and Claude active + parked RateLimitEvent hooks populate limit_store safely."""
    codex_home = str(tmp_path / "codex-home")

    class _FakeStream:
        response = SimpleNamespace(
            headers={
                "X-Codex-Primary-Used-Percent": "73",
                "X-Codex-Primary-Window-Minutes": "300",
                "X-Codex-Secondary-Used-Percent": "bad-value",
            },
        )

        def __aiter__(self) -> _FakeStream:
            return self

        async def __anext__(self) -> Any:  # noqa: ANN401
            raise StopAsyncIteration

        async def close(self) -> None:
            return None

    fake_client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=_FakeStream())))
    adapter = ResponsesAdapter(
        identity=ModelIdentity(id="gpt-5.4", provider="OpenAI Codex", endpoint="https://chatgpt.com/backend-api/codex"),
        capabilities=Capabilities(structured_output="native"),
        wire=ResponsesWire(route="codex", store=False, explicit_breakpoint=False, default_instructions=""),
        settings=ResponsesSettings(codex_home=codex_home),
        clients=SDKClients(lambda: fake_client, per_request=True),
    )
    events = [event async for event in adapter.stream(ModelRequest(messages=[]))]
    assert events == []
    codex_reading = limit_store.get_passive_windows("codex", normalize_codex_scope_key(codex_home))
    assert codex_reading is not None
    assert codex_reading.windows[0].used_percent == 73.0

    # Claude Code active turn hook & parked drain hook both populate limit_store and preserve session.rejected.
    queue: asyncio.Queue[Any] = asyncio.Queue()
    session = LiveSession(
        process=SimpleNamespace(messages=queue),  # type: ignore[arg-type]
        bridge=ToolBridge(names={}),
        provider="Anthropic (Claude Code)",
        cli_path="/fake/claude",
    )
    active_info = RateLimitInfo(
        status="allowed_warning",
        rate_limit_type="five_hour",
        utilization=0.64,
        resets_at=1795472640,
    )
    _Step().accept(RateLimitEvent(rate_limit_info=active_info, uuid="u1", session_id="s1"), session)
    assert session.rejected is active_info

    parked_info = RateLimitInfo(status="rejected", rate_limit_type="seven_day", utilization=None, resets_at=1795615200)
    queue.put_nowait(RateLimitEvent(rate_limit_info=parked_info, uuid="u2", session_id="s1"))
    assert session.broken() is None
    assert session.rejected is parked_info

    claude_reading = limit_store.get_passive_windows("claude_code", "/fake/claude")
    assert claude_reading is not None
    assert {w.id: w.used_percent for w in claude_reading.windows} == {"five_hour": 64.0, "seven_day": 100.0}


@pytest.mark.asyncio
async def test_codex_openrouter_and_payg_probes_and_gateway_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probes Codex wham/usage, OpenRouter capped & uncapped, PAYG, and blocks custom-gateway keys."""
    seen_urls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append((str(request.url), dict(request.headers)))
        if str(request.url) == provider_probes.CODEX_USAGE_URL:
            assert request.headers["authorization"] == "Bearer codex-tok"
            assert request.headers["chatgpt-account-id"] == "acct-42"
            return httpx.Response(
                200,
                json={
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {"used_percent": 88, "limit_window_seconds": 18000, "reset_at": 1795475160},
                        "secondary_window": {
                            "used_percent": 81,
                            "limit_window_seconds": 604800,
                            "reset_at": 1795615200,
                        },
                    },
                },
            )
        if str(request.url) == provider_probes.OPENROUTER_KEY_URL:
            if request.headers["authorization"] == "Bearer sk-or-capped":
                return httpx.Response(
                    200,
                    json={"data": {"limit": 50.0, "limit_remaining": 31.6, "limit_reset": "monthly"}},
                )
            if request.headers["authorization"] == "Bearer sk-or-unlimited":
                return httpx.Response(200, json={"data": {"limit": None, "usage": 12.5}})
        return httpx.Response(500)

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_probes, "borrow_codex_key", lambda **_kw: ("codex-tok", "acct-42"))
    monkeypatch.setattr(provider_limits, "collect_admin_usage", lambda **_kw: _empty_report())

    secret_home = str(tmp_path / "super-secret-codex-dir")
    config = Config(
        models={
            "codex_main": ModelConfig(provider="codex", id="gpt-5.4", extra_kwargs={"codex_home": secret_home}),
            "or_direct": ModelConfig(provider="openrouter", id="google/gemini-3-pro", api_key="sk-or-capped"),
            "or_anthropic_route": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-sonnet-4",
                extra_kwargs={"base_url": "https://openrouter.ai/api", "auth_token": "sk-or-capped"},
            ),
            "or_unlimited": ModelConfig(
                provider="openai",
                id="openai/gpt-5.4",
                api_key="sk-or-unlimited",
                extra_kwargs={"base_url": "https://openrouter.ai/api/v1"},
            ),
            "custom_gw": ModelConfig(
                provider="openrouter",
                id="custom/model",
                api_key="sk-private-gateway-key",
                extra_kwargs={"base_url": "https://gateway.internal/v1"},
            ),
            "direct_google": ModelConfig(provider="gemini", id="gemini-2.5-pro"),
        },
    )
    snapshot = await get_provider_usage_snapshot(
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        now=FIXED_NOW,
    )
    payload_text = json.dumps(snapshot.to_dict())
    # Never leak filesystem paths or API keys.
    assert secret_home not in payload_text
    assert "sk-or-" not in payload_text
    assert "sk-private-gateway-key" not in payload_text
    assert all(headers.get("authorization") != "Bearer sk-private-gateway-key" for _, headers in seen_urls)

    by_id = {acct.id: acct for acct in snapshot.accounts}
    assert by_id["codex:default"].plan == "ChatGPT Pro"
    assert [(w.id, w.remaining_percent) for w in by_id["codex:default"].windows] == [
        ("five_hour", 12.0),
        ("seven_day", 19.0),
    ]
    # or_direct + or_anthropic_route share the same key -> deduplicated into one OpenRouter account.
    or_accounts = [
        acct for acct in snapshot.accounts if acct.id.startswith("openrouter:") and acct.id != "openrouter:custom"
    ]
    assert len(or_accounts) == 2
    capped = next(acct for acct in or_accounts if acct.plan == "Credit cap")
    assert set(capped.model_keys) == {"or_direct", "or_anthropic_route"}
    assert capped.windows[0].detail == "$31.60 / $50.00 left"
    assert capped.windows[0].remaining_percent == 63.2
    uncapped = next(acct for acct in or_accounts if acct.plan == "Unlimited credit")
    assert uncapped.windows == ()
    assert by_id["google:default"].plan == "Pay-as-you-go"
    assert by_id["google:default"].windows == ()
    assert by_id["openrouter:custom"].plan == "Pay-as-you-go"


@pytest.mark.asyncio
async def test_claude_code_fake_cli_isolation_rejection_and_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code probe scrubs env via launcher.py, sends no prompt, rejects API-key auth, and reaps on timeout."""
    record_file = tmp_path / "cli-record.json"
    ok_cli = _make_fake_claude_cli(
        tmp_path,
        f"""
import json, os, sys
from pathlib import Path
seen = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "cwd_entries": os.listdir(os.getcwd()),
        "leaked_env": [k for k in os.environ if k.startswith("ANTHROPIC_") or k == "CLAUDE_CODE_OAUTH_TOKEN"],
        "requests": []}}
for line in sys.stdin:
    msg = json.loads(line)
    seen["requests"].append(msg)
    req_id = msg["request_id"]
    subtype = msg["request"]["subtype"]
    if subtype == "initialize":
        resp = {{"account": {{"apiProvider": "firstParty", "subscriptionType": "Claude Max 20x", "apiKeySource": "claude.ai"}}}}
    elif subtype == "get_usage":
        resp = {{"rate_limits": {{
            "five_hour": {{"utilization": 38.0, "resets_at": "2026-11-23T22:24:00Z"}},
            "seven_day": {{"utilization": 22.0, "resets_at": "2026-11-26T09:00:00Z"}},
            "model_scoped": {{"sonnet": {{"utilization": 15.0, "resets_at": "2026-11-26T09:00:00Z"}}}},
            "extra_usage": {{"is_enabled": True, "used_credits": 1250, "monthly_limit": 5000}},
        }}}}
    else:
        resp = {{}}
    print(json.dumps({{"type": "control_response", "response": {{"subtype": "success", "request_id": req_id, "response": resp}}}}), flush=True)
    Path({str(record_file)!r}).write_text(json.dumps(seen), encoding="utf-8")
""",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-be-scrubbed")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-must-be-scrubbed")
    monkeypatch.setattr(provider_limits, "collect_admin_usage", lambda **_kw: _empty_report())

    config = Config(
        models={
            "opus": ModelConfig(provider="claude_code", id="claude-opus-4-6", extra_kwargs={"cli_path": str(ok_cli)}),
        },
    )
    snapshot = await get_provider_usage_snapshot(
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        now=FIXED_NOW,
    )

    acct = snapshot.accounts[0]
    assert acct.plan == "Max 20x"
    assert acct.stale is False
    assert [(w.id, w.used_percent) for w in acct.windows] == [
        ("five_hour", 38.0),
        ("seven_day", 22.0),
        ("seven_day_sonnet", 15.0),
        ("extra_usage", 25.0),
    ]
    recorded = json.loads(record_file.read_text(encoding="utf-8"))
    assert recorded["leaked_env"] == []
    assert recorded["cwd_entries"] == []
    assert "--no-session-persistence" in recorded["argv"]
    assert "--strict-mcp-config" in recorded["argv"]
    assert [req["request"]["subtype"] for req in recorded["requests"]] == ["initialize", "get_usage"]
    assert all(req["type"] == "control_request" for req in recorded["requests"])

    # API-key login must be rejected before get_usage is ever sent.
    apikey_log = tmp_path / "apikey-log.json"
    apikey_cli = _make_fake_claude_cli(
        tmp_path / "apikey_dir" if (tmp_path / "apikey_dir").mkdir() or True else tmp_path,
        f"""
import json, sys
from pathlib import Path
subtypes = []
for line in sys.stdin:
    msg = json.loads(line)
    subtypes.append(msg["request"]["subtype"])
    resp = {{"account": {{"apiProvider": "firstParty", "apiKeySource": "ANTHROPIC_API_KEY"}}}}
    print(json.dumps({{"type": "control_response", "response": {{"subtype": "success", "request_id": msg["request_id"], "response": resp}}}}), flush=True)
    Path({str(apikey_log)!r}).write_text(json.dumps(subtypes), encoding="utf-8")
""",
    )
    res_unsupported = await provider_probes.probe_claude_code(str(apikey_cli), FIXED_NOW)
    assert res_unsupported.unavailable_reason == "unsupported"
    assert json.loads(apikey_log.read_text(encoding="utf-8")) == ["initialize"]

    # Hung CLI times out, reaps child process, and falls back to passive reading with stale=True.
    pid_file = tmp_path / "hung.pid"
    hung_dir = tmp_path / "hung_dir"
    hung_dir.mkdir()
    hung_cli = _make_fake_claude_cli(
        hung_dir,
        f"""
import os, time
from pathlib import Path
Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
""",
    )
    monkeypatch.setattr(provider_probes, "CLAUDE_PROBE_TIMEOUT_SECONDS", 0.25)
    limit_store.record_claude_rate_limit(
        str(hung_cli.resolve()),
        rate_limit_type="five_hour",
        utilization=0.55,
        resets_at=1795472640,
        now=FIXED_NOW,
    )
    hung_cfg = Config(
        models={
            "opus": ModelConfig(provider="claude_code", id="claude-opus-4-6", extra_kwargs={"cli_path": str(hung_cli)}),
        },
    )
    hung_snap = await get_provider_usage_snapshot(
        config=hung_cfg,
        runtime_paths=test_runtime_paths(tmp_path),
        now=FIXED_NOW,
    )
    assert hung_snap.accounts[0].stale is True
    assert hung_snap.accounts[0].windows[0].used_percent == 55.0
    if pid_file.exists():
        hung_pid = int(pid_file.read_text(encoding="utf-8"))
        with pytest.raises(ProcessLookupError):
            os.kill(hung_pid, 0)


@pytest.mark.asyncio
async def test_7d_filtering_ambiguity_caching_stale_retention_and_budget_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """7-day UTC filtering, multi-account unattributed tokens, TTL/cooldown/single-flight, stale fallback, and 48 KB cap."""
    daily = (
        # 8 days ago -> excluded from 7d window (Nov 17..23).
        UsageDailyBreakdownRow(
            date="2026-11-16",
            totals=TokenCounts(input_tokens=500, output_tokens=500, total_tokens=1000),
            run_count=1,
            model_breakdown=(
                UsageModelBreakdownRow(
                    model_provider="Anthropic",
                    model="claude-haiku-4-5",
                    totals=TokenCounts(input_tokens=500, output_tokens=500, total_tokens=1000),
                    run_count=1,
                ),
            ),
        ),
        UsageDailyBreakdownRow(
            date="2026-11-23",
            totals=TokenCounts(input_tokens=3000, output_tokens=1000, total_tokens=4000),
            run_count=2,
            model_breakdown=(
                UsageModelBreakdownRow(
                    model_provider="Anthropic",
                    model="claude-haiku-4-5",
                    totals=TokenCounts(input_tokens=1000, output_tokens=500, total_tokens=1500),
                    run_count=1,
                ),
                # Shared across two codex accounts -> unattributed, never double-counted.
                UsageModelBreakdownRow(
                    model_provider="OpenAI Codex",
                    model="gpt-5.4",
                    totals=TokenCounts(input_tokens=2000, output_tokens=500, total_tokens=2500),
                    run_count=1,
                ),
            ),
        ),
    )
    monkeypatch.setattr(provider_limits, "collect_admin_usage", lambda **_kw: _empty_report(daily))

    now_mono = [100.0]
    monkeypatch.setattr(provider_limits, "_monotonic", lambda: now_mono[0])
    probe_calls = 0
    should_fail = [False]

    async def _fake_codex(_codex_home: str | None, now: datetime) -> provider_probes.ProbeResult:
        nonlocal probe_calls
        probe_calls += 1
        await asyncio.sleep(0.02)
        if should_fail[0]:
            return provider_probes.ProbeResult(unavailable_reason="probe_failed")
        return provider_probes.ProbeResult(
            windows=(build_window(window_id="five_hour", kind="session", label="5-hour window", used_percent=20.0),),
            plan="ChatGPT Pro",
            account_label="ChatGPT Pro",
            observed_at=now,
            succeeded=True,
        )

    monkeypatch.setattr(provider_limits, "probe_codex", _fake_codex)
    monkeypatch.setattr(
        provider_probes,
        "borrow_codex_key",
        lambda **_kw: (_ for _ in ()).throw(CodexAuthError("unused")),
    )

    config = Config(
        models={
            "codex_a": ModelConfig(provider="codex", id="gpt-5.4", extra_kwargs={"codex_home": str(tmp_path / "c1")}),
            "codex_b": ModelConfig(provider="codex", id="gpt-5.4", extra_kwargs={"codex_home": str(tmp_path / "c2")}),
            "haiku": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                pricing=ModelPricing(input=1.0, output=5.0),
            ),
        },
    )
    paths = test_runtime_paths(tmp_path)

    # Concurrent cold callers deduplicate via single-flight lock (only 2 codex accounts probed once each).
    s1, s2 = await asyncio.gather(
        get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW),
        get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW),
    )
    assert s1 == s2
    assert probe_calls == 2
    by_id = {a.id: a for a in s1.accounts}
    assert by_id["anthropic:default"].tokens_7d == 1500
    assert by_id["anthropic:default"].cost_usd_7d == pytest.approx(0.0035)
    assert by_id["anthropic:default"].cost_estimated is True
    assert by_id["codex:default"].tokens_7d == 0
    assert by_id["codex:2"].tokens_7d == 0
    assert s1.unattributed_tokens_7d == 2500
    assert [row.day for row in s1.daily_7d] == ["2026-11-23"]
    assert s1.daily_7d[0].unattributed_tokens == 2500

    # Within 10s cooldown, even force_refresh returns cached snapshot.
    now_mono[0] = 105.0
    await get_provider_usage_snapshot(config=config, runtime_paths=paths, force_refresh=True, now=FIXED_NOW)
    assert probe_calls == 2

    # After 10s cooldown, force_refresh re-probes; on failure it retains last-good reading with stale=True.
    now_mono[0] = 115.0
    should_fail[0] = True
    s3 = await get_provider_usage_snapshot(config=config, runtime_paths=paths, force_refresh=True, now=FIXED_NOW)
    assert probe_calls == 4
    stale_codex = {a.id: a for a in s3.accounts}["codex:default"]
    assert stale_codex.stale is True
    assert stale_codex.windows[0].used_percent == 20.0

    # Codex falls back to passive limit_store windows on probe failure when no prior probe succeeded.
    provider_limits.clear_cache()
    passive_home = str(tmp_path / "passive-codex")
    limit_store.record_codex_headers(
        normalize_codex_scope_key(passive_home),
        {"x-codex-primary-used-percent": "61", "x-codex-primary-window-minutes": "300"},
        now=FIXED_NOW,
    )
    passive_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"CODEX_HOME": passive_home},
    )
    passive_cfg = Config(
        models={
            "codex_env": ModelConfig(provider="codex", id="gpt-5.4"),
            "or_missing": ModelConfig(provider="openrouter", id="google/gemini-3-pro"),
        },
    )
    s_passive = await get_provider_usage_snapshot(config=passive_cfg, runtime_paths=passive_paths, now=FIXED_NOW)
    passive_by_id = {a.id: a for a in s_passive.accounts}
    assert passive_by_id["codex:default"].stale is True
    assert passive_by_id["codex:default"].windows[0].used_percent == 61.0
    assert passive_by_id["openrouter:1"].unavailable_reason == "not_signed_in"


def test_fit_to_device_budget_drops_daily_then_trims_model_keys() -> None:
    """48 KB budget cap drops daily_7d first, then trims model_keys, setting partial=True."""
    oversized: dict[str, object] = {
        "version": 1,
        "request_id": "req-big",
        "checked_at": "2026-11-23T20:00:00Z",
        "partial": False,
        "accounts": [
            {
                "id": "anthropic:default",
                "provider": "anthropic",
                "display_name": "Anthropic",
                "model_keys": [f"model-key-{i:04d}" for i in range(400)],
                "windows": [],
                "stale": False,
                "tokens_7d": 0,
                "cost_usd_7d": None,
                "cost_estimated": False,
            },
        ],
        "daily_7d": [
            {"day": f"2026-11-{d:02d}", "by_account": {"a": d}, "unattributed_tokens": 0} for d in range(1, 30)
        ],
        "unattributed_tokens_7d": 0,
        "unattributed_cost_usd_7d": None,
    }
    trimmed = fit_to_device_budget(oversized, max_bytes=1200)
    assert trimmed["partial"] is True
    assert trimmed["daily_7d"] == []
    assert len(json.dumps(trimmed).encode("utf-8")) <= 1200
    assert len(trimmed["accounts"][0]["model_keys"]) < 400
