"""Tests for passive limit capture, provider probes, 7-day attribution, caching, and budgeting."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.llm.provider import Capabilities, ModelIdentity, ModelRequest
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.providers.claude_code.bridge import ToolBridge
from mindroom.providers.claude_code.session import LiveSession
from mindroom.providers.claude_code.turn import _Step
from mindroom.providers.openai.adapter_common import SDKClients
from mindroom.providers.openai.codex_auth import CodexAuthError
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import ResponsesWire
from mindroom.providers.openai.settings import ResponsesSettings
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.usage import limit_store, provider_limits, provider_probes
from mindroom.usage.limit_store import PassiveWindow, build_window, normalize_codex_scope_key
from mindroom.usage.provider_limits import (
    AgentUsage7d,
    DailyAccountUsage,
    ModelUsage7d,
    ProviderLimitAccount,
    ProviderUsageSnapshot,
    TokenMix,
    fit_to_device_budget,
    get_provider_usage_snapshot,
)
from mindroom.usage.stats import (
    UsageDailyBreakdownRow,
    UsageModelBreakdownRow,
)
from tests.conftest import persist_runs, test_runtime_paths

CONTRACT_FIXTURE = Path(__file__).parent / "fixtures" / "provider_usage_contract.json"
FIXED_NOW = datetime(2026, 11, 23, 20, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_stores() -> None:
    limit_store.clear()
    provider_limits.clear_cache()


type _DailyScan = tuple[tuple[UsageDailyBreakdownRow, ...], dict[str, tuple[UsageDailyBreakdownRow, ...]]]


def _empty_daily_usage(
    daily: tuple[UsageDailyBreakdownRow, ...] = (),
    by_entity: dict[str, tuple[UsageDailyBreakdownRow, ...]] | None = None,
) -> _DailyScan:
    return daily, dict(by_entity or {})


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
                token_mix_7d=TokenMix(12400000, 1800000, 18500000, 2100000),
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
                token_mix_7d=TokenMix(1800000, 700000, 7300000, 0),
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
                token_mix_7d=TokenMix(2100000, 900000, 3400000, 0),
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
                token_mix_7d=TokenMix(900000, 300000, 2400000, 350000),
            ),
        ),
        daily_7d=(
            DailyAccountUsage(
                day="2026-11-17",
                by_account={"claude_code:default": 1900000, "codex:default": 1200000, "openrouter:1": 900000},
                unattributed_tokens=0,
                cost_by_account={"openrouter:1": 2.6},
            ),
            DailyAccountUsage(
                day="2026-11-18",
                by_account={"claude_code:default": 2100000, "codex:default": 1500000, "openrouter:1": 850000},
                unattributed_tokens=0,
                cost_by_account={"openrouter:1": 2.45},
            ),
            DailyAccountUsage(
                day="2026-11-20",
                by_account={
                    "anthropic:default": 400000,
                    "claude_code:default": 3400000,
                    "codex:default": 2300000,
                    "openrouter:1": 1550000,
                },
                unattributed_tokens=0,
                cost_by_account={"anthropic:default": 1.08, "openrouter:1": 4.45},
            ),
            DailyAccountUsage(
                day="2026-11-22",
                by_account={
                    "anthropic:default": 450000,
                    "claude_code:default": 3600000,
                    "codex:default": 2500000,
                    "openrouter:1": 1600000,
                },
                unattributed_tokens=0,
                cost_by_account={"anthropic:default": 1.22, "openrouter:1": 4.6},
            ),
            DailyAccountUsage(
                day="2026-11-23",
                by_account={
                    "anthropic:default": 350000,
                    "claude_code:default": 3200000,
                    "codex:default": 2300000,
                    "openrouter:1": 1500000,
                },
                unattributed_tokens=0,
                cost_by_account={"anthropic:default": 0.95, "openrouter:1": 4.3},
            ),
        ),
        unattributed_tokens_7d=0,
        unattributed_cost_usd_7d=None,
        token_mix_7d=TokenMix(17200000, 3700000, 31600000, 2450000),
        cache_savings_usd_7d=6.85,
        top_models_7d=(
            ModelUsage7d(
                provider="OpenRouter",
                model="google/gemini-3-pro",
                account_id="openrouter:1",
                tokens_7d=6400000,
                cost_usd_7d=18.4,
                run_count=142,
                token_mix_7d=TokenMix(2100000, 900000, 3400000, 0),
                daily_tokens_7d=(900000, 850000, 1550000, 1600000, 1500000),
            ),
            ModelUsage7d(
                provider="Anthropic",
                model="claude-haiku-4-5",
                account_id="anthropic:default",
                tokens_7d=1200000,
                cost_usd_7d=3.25,
                run_count=68,
                token_mix_7d=TokenMix(900000, 300000, 2400000, 350000),
                daily_tokens_7d=(0, 0, 400000, 450000, 350000),
            ),
            ModelUsage7d(
                provider="Anthropic (Claude Code)",
                model="claude-opus-4-6",
                account_id="claude_code:default",
                tokens_7d=14200000,
                cost_usd_7d=None,
                run_count=210,
                token_mix_7d=TokenMix(12400000, 1800000, 18500000, 2100000),
                daily_tokens_7d=(1900000, 2100000, 3400000, 3600000, 3200000),
            ),
            ModelUsage7d(
                provider="OpenAI Codex",
                model="gpt-5.4",
                account_id="codex:default",
                tokens_7d=9800000,
                cost_usd_7d=None,
                run_count=154,
                token_mix_7d=TokenMix(1800000, 700000, 7300000, 0),
                daily_tokens_7d=(1200000, 1500000, 2300000, 2500000, 2300000),
            ),
        ),
        top_agents_7d=(
            AgentUsage7d(
                entity="code",
                tokens_7d=18400000,
                cost_usd_7d=14.2,
                run_count=310,
                token_mix_7d=TokenMix(10000000, 2150000, 18400000, 1450000),
                daily_tokens_7d=(2300000, 2600000, 4450000, 4750000, 4300000),
            ),
            AgentUsage7d(
                entity="research",
                tokens_7d=8900000,
                cost_usd_7d=7.45,
                run_count=164,
                token_mix_7d=TokenMix(4850000, 1050000, 8900000, 650000),
                daily_tokens_7d=(1150000, 1250000, 2150000, 2300000, 2050000),
            ),
            AgentUsage7d(
                entity="general",
                tokens_7d=4300000,
                cost_usd_7d=None,
                run_count=100,
                token_mix_7d=TokenMix(2350000, 500000, 4300000, 350000),
                daily_tokens_7d=(550000, 600000, 1050000, 1100000, 1000000),
            ),
        ),
    )
    assert snapshot.to_dict(request_id="req-provider-usage-1") == expected
    rest_payload = snapshot.to_dict()
    assert "request_id" not in rest_payload


def test_limit_store_codex_and_claude_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passive store parses case-insensitive Codex headers, Claude buckets, enforces bounded LRU, and ignores NaN."""
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

    # Bounded LRU promotes accessed entries and evicts the least recently used scope.
    limit_store.clear()
    monkeypatch.setattr(limit_store, "_MAX_SCOPES", 2)
    for scope in ("scope-1", "scope-2"):
        limit_store.record_codex_headers(scope, {"x-codex-primary-used-percent": "10"}, now=FIXED_NOW)
    assert limit_store.get_passive_windows("codex", "scope-1") is not None
    limit_store.record_codex_headers("scope-3", {"x-codex-primary-used-percent": "20"}, now=FIXED_NOW)
    assert limit_store.get_passive_windows("codex", "scope-2") is None
    assert limit_store.get_passive_windows("codex", "scope-1") is not None
    assert limit_store.get_passive_windows("codex", "scope-3") is not None


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
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())

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
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())

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
    assert "--settings" in recorded["argv"]
    assert '{"disableAllHooks":true}' in recorded["argv"]
    assert [req["request"]["subtype"] for req in recorded["requests"]] == ["initialize", "get_usage"]
    assert recorded["requests"][1]["request"].get("skip_behaviors") is True
    assert all(req["type"] == "control_request" for req in recorded["requests"])

    # API-key login must be rejected before get_usage is ever sent.
    apikey_log = tmp_path / "apikey-log.json"
    apikey_dir = tmp_path / "apikey_dir"
    apikey_dir.mkdir()
    apikey_cli = _make_fake_claude_cli(
        apikey_dir,
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
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage(daily),
    )

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


def test_frontend_contract_fixture_matches_backend_byte_for_byte() -> None:
    """Dashboard frontend contract fixture stays byte-identical to backend fixture."""
    frontend_fixture = (
        Path(__file__).resolve().parents[1]
        / "frontend"
        / "src"
        / "components"
        / "Usage"
        / "__fixtures__"
        / "providerUsageContract.json"
    )
    assert frontend_fixture.read_bytes() == CONTRACT_FIXTURE.read_bytes()


@pytest.mark.asyncio
async def test_openrouter_auth_header_override_and_zero_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorization header override beats OPENAI_API_KEY; non-Bearer skips probe; limit==0 is $0 cap."""
    seen_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen_auth.append(auth)
        if auth == "Bearer sk-or-header-tok":
            return httpx.Response(200, json={"data": {"limit": 0, "limit_remaining": 0, "limit_reset": "monthly"}})
        return httpx.Response(500)

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"OPENAI_API_KEY": "sk-openai-shared-never-send"},
    )
    config = Config(
        models={
            "or_bearer_hdr": ModelConfig(
                provider="openai",
                id="openai/gpt-5.4",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api/v1",
                    "extra_headers": {"Authorization": "Bearer sk-or-header-tok"},
                },
            ),
            "or_non_bearer": ModelConfig(
                provider="openai",
                id="openai/gpt-5.4-mini",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api/v1",
                    "default_headers": {"Authorization": "Basic dXNlcjpwYXNz"},
                },
            ),
        },
    )
    snapshot = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert seen_auth == ["Bearer sk-or-header-tok"]
    assert all("sk-openai-shared-never-send" not in h for h in seen_auth)
    by_id = {acct.id: acct for acct in snapshot.accounts}
    assert by_id["openrouter:1"].plan == "Credit cap"
    assert by_id["openrouter:1"].windows[0].remaining_percent == 0.0
    assert by_id["openrouter:1"].windows[0].detail == "$0.00 / $0.00 left"
    assert by_id["openrouter:custom"].plan == "Pay-as-you-go"
    assert by_id["openrouter:custom"].windows == ()


@pytest.mark.asyncio
async def test_claude_get_usage_low_percent_display_name_null_limits_and_unrelated_edit_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active get_usage keeps 0.5 as 0.5%, uses model_scoped display_name, and preserves stale on null rate_limits."""
    parsed = provider_probes._parse_claude_usage(
        {
            "rate_limits_available": True,
            "rate_limits": {
                "five_hour": {"utilization": 0.5, "resets_at": "2026-11-23T22:00:00Z"},
                "model_scoped": [
                    {"display_name": "Sonnet", "utilization": 12.5, "resets_at": "2026-11-26T09:00:00Z"},
                ],
            },
        },
    )
    assert not isinstance(parsed, str)
    assert [(w.id, w.label, w.used_percent, w.remaining_percent) for w in parsed] == [
        ("five_hour", "5-hour window", 0.5, 99.5),
        ("seven_day_sonnet", "7-day Sonnet", 12.5, 87.5),
    ]
    assert provider_probes._parse_claude_usage({"rate_limits_available": True, "rate_limits": None}) == "probe_failed"
    assert provider_probes._parse_claude_usage({"rate_limits_available": False, "rate_limits": None}) == "unsupported"

    # Probe succeeds once, then unrelated model edit invalidates cache; next probe returns null rate_limits -> stale preserved.
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    mode = ["ok"]

    async def _fake_claude(_cli: str | None, now: datetime) -> provider_probes.ProbeResult:
        if mode[0] == "ok":
            return provider_probes.ProbeResult(
                windows=parsed,
                plan="Max 20x",
                account_label="Max 20x",
                observed_at=now,
                succeeded=True,
            )
        return provider_probes.ProbeResult(unavailable_reason="probe_failed")

    monkeypatch.setattr(provider_limits, "probe_claude_code", _fake_claude)
    paths = test_runtime_paths(tmp_path)
    cfg1 = Config(models={"opus": ModelConfig(provider="claude_code", id="claude-opus-4-6")})
    snap1 = await get_provider_usage_snapshot(config=cfg1, runtime_paths=paths, now=FIXED_NOW)
    assert snap1.accounts[0].stale is False
    assert snap1.accounts[0].windows[0].used_percent == 0.5

    mode[0] = "null"
    cfg2 = Config(
        models={
            "opus": ModelConfig(provider="claude_code", id="claude-opus-4-6"),
            "haiku": ModelConfig(provider="anthropic", id="claude-haiku-4-5"),
        },
    )
    snap2 = await get_provider_usage_snapshot(config=cfg2, runtime_paths=paths, now=FIXED_NOW)
    claude_acct = {a.id: a for a in snap2.accounts}["claude_code:default"]
    assert claude_acct.stale is True
    assert claude_acct.windows[0].used_percent == 0.5


@pytest.mark.asyncio
async def test_openrouter_request_params_authorization_and_x_api_key_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_params.extra_headers.Authorization and default_headers.X-Api-Key override shared keys; conflicts skip."""
    seen_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen_auth.append(auth)
        if auth in {"Bearer sk-or-req-wins", "Bearer sk-or-xkey-tok"}:
            return httpx.Response(200, json={"data": {"limit": 10.0, "limit_remaining": 7.5, "limit_reset": "monthly"}})
        return httpx.Response(500)

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "OPENAI_API_KEY": "sk-openai-shared-never-send",
            "ANTHROPIC_API_KEY": "sk-anthropic-shared-never-send",
        },
    )
    config = Config(
        models={
            "or_req_auth": ModelConfig(
                provider="openai",
                id="openai/gpt-5.4",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api/v1",
                    "extra_headers": {"Authorization": "Bearer sk-or-shadowed"},
                    "request_params": {"extra_headers": {"Authorization": "Bearer sk-or-req-wins"}},
                },
            ),
            "or_ant_xkey": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-sonnet-4-6",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api",
                    "default_headers": {"X-Api-Key": "sk-or-xkey-tok"},
                },
            ),
            "or_conflict": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-haiku-4-5",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api",
                    "default_headers": {"Authorization": "Bearer sk-or-a", "X-Api-Key": "sk-or-b"},
                },
            ),
            "or_unreadable_xkey": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-opus-4-6",
                extra_kwargs={
                    "base_url": "https://openrouter.ai/api",
                    "default_headers": {"X-Api-Key": "${UNRESOLVED_KEY}"},
                },
            ),
        },
    )
    snapshot = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert sorted(seen_auth) == ["Bearer sk-or-req-wins", "Bearer sk-or-xkey-tok"]
    for leaked in (
        "sk-openai-shared-never-send",
        "sk-anthropic-shared-never-send",
        "sk-or-shadowed",
        "sk-or-a",
        "sk-or-b",
    ):
        assert all(leaked not in header for header in seen_auth)
    by_id = {acct.id: acct for acct in snapshot.accounts}
    assert by_id["openrouter:1"].plan == "Credit cap"
    assert by_id["openrouter:2"].plan == "Credit cap"
    assert by_id["openrouter:custom"].plan == "Pay-as-you-go"
    assert by_id["openrouter:custom"].model_keys == ("or_conflict", "or_unreadable_xkey")


@pytest.mark.asyncio
async def test_payg_models_group_by_provider_route_without_leaking_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-OpenRouter PAYG models group cleanly by provider route (<route>:default) and never leak keys/projects."""
    daily = (
        UsageDailyBreakdownRow(
            date="2026-11-23",
            totals=TokenCounts(input_tokens=2000, output_tokens=500, total_tokens=2500),
            run_count=1,
            model_breakdown=(
                UsageModelBreakdownRow(
                    model_provider="Anthropic",
                    model="claude-haiku-4-5",
                    totals=TokenCounts(input_tokens=2000, output_tokens=500, total_tokens=2500),
                    run_count=1,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage(daily),
    )
    config = Config(
        models={
            "haiku_a": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                api_key="sk-ant-key-one",
                pricing=ModelPricing(input=1.0, output=5.0),
            ),
            "haiku_b": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                api_key="sk-ant-key-two",
            ),
            "vertex_a": ModelConfig(
                provider="google",
                id="gemini-2.5-pro",
                extra_kwargs={"vertexai": True, "project_id": "gcp-proj-alpha"},
            ),
            "vertex_b": ModelConfig(
                provider="gemini",
                id="gemini-2.5-flash",
                extra_kwargs={"vertexai": True, "client_params": {"project": "gcp-proj-beta"}},
            ),
        },
    )
    snapshot = await get_provider_usage_snapshot(
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        now=FIXED_NOW,
    )
    payload_text = json.dumps(snapshot.to_dict())
    for secret in ("sk-ant-key-", "gcp-proj-alpha", "gcp-proj-beta"):
        assert secret not in payload_text
    by_id = {acct.id: acct for acct in snapshot.accounts}
    assert set(by_id) == {"anthropic:default", "google:default"}
    assert by_id["anthropic:default"].model_keys == ("haiku_a", "haiku_b")
    assert by_id["anthropic:default"].tokens_7d == 2500
    assert by_id["anthropic:default"].cost_usd_7d == pytest.approx(0.0045)
    assert by_id["google:default"].model_keys == ("vertex_a", "vertex_b")
    assert snapshot.unattributed_tokens_7d == 0


@pytest.mark.asyncio
async def test_snapshot_bounds_usage_scan_since_and_shields_single_flight_from_caller_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collect_admin_usage is bounded with since=7d UTC ago, and a caller timeout does not cancel the shared scan."""
    seen_since: list[float | None] = []
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    probe_calls = 0

    def _fake_collect(**kwargs: object) -> _DailyScan:
        since = kwargs.get("since")
        seen_since.append(since if isinstance(since, float) else None)
        return _empty_daily_usage()

    async def _slow_probe(_cli: str | None, now: datetime) -> provider_probes.ProbeResult:
        nonlocal probe_calls
        probe_calls += 1
        probe_started.set()
        await release_probe.wait()
        return provider_probes.ProbeResult(
            windows=(
                limit_store.build_window(
                    window_id="five_hour",
                    kind="session",
                    label="5-hour window",
                    used_percent=20.0,
                    window_duration_mins=300,
                ),
            ),
            plan="Max 20x",
            account_label="Max 20x",
            observed_at=now,
            succeeded=True,
        )

    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", _fake_collect)
    monkeypatch.setattr(provider_limits, "probe_claude_code", _slow_probe)
    config = Config(models={"opus": ModelConfig(provider="claude_code", id="claude-opus-4-6")})
    paths = test_runtime_paths(tmp_path)

    caller = asyncio.create_task(get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW))
    await probe_started.wait()
    # Timeout on a second waiter and cancellation on the first caller must not cancel the shielded scan.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0):
            await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    inflight_tasks = list(provider_limits._IN_FLIGHT.get(asyncio.get_running_loop(), {}).values())
    assert len(inflight_tasks) == 1
    assert not inflight_tasks[0].cancelled()
    release_probe.set()
    await inflight_tasks[0]

    # After all callers left, the shielded task still populated _CACHE so the next call is an immediate cache hit.
    snapshot = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert probe_calls == 1
    assert snapshot.accounts[0].plan == "Max 20x"
    assert snapshot.accounts[0].windows[0].used_percent == 20.0
    assert seen_since == [datetime(2026, 11, 16, 0, 0, tzinfo=UTC).timestamp()]


@pytest.mark.asyncio
async def test_openrouter_anthropic_custom_headers_and_env_auth_token_never_leak_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTHROPIC_CUSTOM_HEADERS and ANTHROPIC_AUTH_TOKEN overrides never leak ANTHROPIC_API_KEY to OpenRouter."""
    seen_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen_auth.append(auth)
        if auth in {"Bearer gw", "Bearer tok-or-env"}:
            return httpx.Response(
                200,
                json={"data": {"limit": 20.0, "limit_remaining": 15.0, "limit_reset": "monthly"}},
            )
        return httpx.Response(500)

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    config = Config(
        models={
            "or_claude": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-sonnet-4-6",
                extra_kwargs={"base_url": "https://openrouter.ai/api"},
            ),
        },
    )

    # Case 1: ANTHROPIC_CUSTOM_HEADERS="X-Api-Key: gw" overrides ANTHROPIC_API_KEY.
    paths_custom = resolve_primary_runtime_paths(
        config_path=tmp_path / "custom" / "config.yaml",
        storage_path=tmp_path / "custom" / "storage",
        process_env={
            "ANTHROPIC_API_KEY": "sk-ant-shared-never-send",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Api-Key: gw",
        },
    )
    snap_custom = await get_provider_usage_snapshot(config=config, runtime_paths=paths_custom, now=FIXED_NOW)
    assert seen_auth == ["Bearer gw"]
    assert all("sk-ant-shared-never-send" not in header for header in seen_auth)
    assert snap_custom.accounts[0].id == "openrouter:1"
    assert snap_custom.accounts[0].plan == "Credit cap"

    # Case 2: Conflicting ANTHROPIC_CUSTOM_HEADERS skips probe cleanly without leaking ANTHROPIC_API_KEY.
    seen_auth.clear()
    paths_conflict = resolve_primary_runtime_paths(
        config_path=tmp_path / "conflict" / "config.yaml",
        storage_path=tmp_path / "conflict" / "storage",
        process_env={
            "ANTHROPIC_API_KEY": "sk-ant-shared-never-send",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Api-Key: gw\nAuthorization: Bearer other",
        },
    )
    snap_conflict = await get_provider_usage_snapshot(config=config, runtime_paths=paths_conflict, now=FIXED_NOW)
    assert seen_auth == []
    assert snap_conflict.accounts[0].id == "openrouter:custom"
    assert snap_conflict.accounts[0].plan == "Pay-as-you-go"

    # Case 3: Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN set -> uncertain env override skips probe.
    paths_both_env = resolve_primary_runtime_paths(
        config_path=tmp_path / "both" / "config.yaml",
        storage_path=tmp_path / "both" / "storage",
        process_env={
            "ANTHROPIC_API_KEY": "sk-ant-shared-never-send",
            "ANTHROPIC_AUTH_TOKEN": "tok-or-env",
        },
    )
    snap_both_env = await get_provider_usage_snapshot(config=config, runtime_paths=paths_both_env, now=FIXED_NOW)
    assert seen_auth == []
    assert snap_both_env.accounts[0].id == "openrouter:custom"

    # Case 4: Only ANTHROPIC_AUTH_TOKEN set -> unambiguous Bearer token probes OpenRouter.
    paths_token_only = resolve_primary_runtime_paths(
        config_path=tmp_path / "tok" / "config.yaml",
        storage_path=tmp_path / "tok" / "storage",
        process_env={"ANTHROPIC_AUTH_TOKEN": "tok-or-env"},
    )
    snap_token_only = await get_provider_usage_snapshot(config=config, runtime_paths=paths_token_only, now=FIXED_NOW)
    assert seen_auth == ["Bearer tok-or-env"]
    assert snap_token_only.accounts[0].id == "openrouter:1"


@pytest.mark.asyncio
async def test_openrouter_anthropic_sdk_env_overrides_ignore_dotenv_and_secret_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ANTHROPIC_CUSTOM_HEADERS and ANTHROPIC_AUTH_TOKEN take effect only from process_env, not .env or *_FILE."""
    seen_auth: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        seen_auth.append(auth)
        if auth in {"Bearer sk-or-real", "Bearer sk-or-model", "Bearer proc-gw", "Bearer tok-proc-env"}:
            return httpx.Response(
                200,
                json={"data": {"limit": 20.0, "limit_remaining": 15.0, "limit_reset": "monthly"}},
            )
        return httpx.Response(500)

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    config = Config(
        models={
            "or_claude": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-sonnet-4-6",
                extra_kwargs={"base_url": "https://openrouter.ai/api"},
            ),
        },
    )
    config_with_key = Config(
        models={
            "or_claude": ModelConfig(
                provider="anthropic",
                id="anthropic/claude-sonnet-4-6",
                extra_kwargs={"base_url": "https://openrouter.ai/api", "api_key": "sk-or-model"},
            ),
        },
    )

    async def _probe(
        name: str,
        *,
        cfg: Config = config,
        process_env: dict[str, str],
        dotenv_text: str | None = None,
    ) -> tuple[list[str], ProviderLimitAccount]:
        seen_auth.clear()
        case_dir = tmp_path / name
        case_dir.mkdir(parents=True, exist_ok=True)
        if dotenv_text is not None:
            (case_dir / ".env").write_text(dotenv_text, encoding="utf-8")
        paths = resolve_primary_runtime_paths(
            config_path=case_dir / "config.yaml",
            storage_path=case_dir / "storage",
            process_env=process_env,
        )
        if dotenv_text is not None:
            assert paths.env_file_values
        snap = await get_provider_usage_snapshot(config=cfg, runtime_paths=paths, now=FIXED_NOW)
        return list(seen_auth), snap.accounts[0]

    headers_file = tmp_path / "custom_headers.txt"
    headers_file.write_text("X-Api-Key: file-gw\n", encoding="utf-8")
    auth_token_file = tmp_path / "auth_token.txt"
    auth_token_file.write_text("tok-file-gw\n", encoding="utf-8")

    # .env ANTHROPIC_CUSTOM_HEADERS and ANTHROPIC_AUTH_TOKEN are ignored; ANTHROPIC_API_KEY still probes.
    auth, acct = await _probe(
        "dotenv",
        process_env={},
        dotenv_text=(
            "ANTHROPIC_API_KEY=sk-or-real\n"
            "ANTHROPIC_CUSTOM_HEADERS=X-Api-Key: other-gw\n"
            "ANTHROPIC_AUTH_TOKEN=tok-other-gw\n"
        ),
    )
    assert auth == ["Bearer sk-or-real"]
    assert acct.id == "openrouter:1"

    # *_FILE variants in process_env are ignored; ANTHROPIC_API_KEY still probes.
    auth, acct = await _probe(
        "files",
        process_env={
            "ANTHROPIC_API_KEY": "sk-or-real",
            "ANTHROPIC_CUSTOM_HEADERS_FILE": str(headers_file),
            "ANTHROPIC_AUTH_TOKEN_FILE": str(auth_token_file),
        },
    )
    assert auth == ["Bearer sk-or-real"]
    assert acct.id == "openrouter:1"

    # Without ANTHROPIC_API_KEY, .env / *_FILE ANTHROPIC_AUTH_TOKEN must not be sent to OpenRouter.
    auth, acct = await _probe(
        "dotenv-token-only",
        process_env={"ANTHROPIC_AUTH_TOKEN_FILE": str(auth_token_file)},
        dotenv_text="ANTHROPIC_AUTH_TOKEN=tok-other-gw\n",
    )
    assert auth == []
    assert acct.unavailable_reason == "not_signed_in"

    # Explicit model api_key is not overridden by .env ANTHROPIC_CUSTOM_HEADERS or *_FILE.
    auth, acct = await _probe(
        "model-key",
        cfg=config_with_key,
        process_env={"ANTHROPIC_CUSTOM_HEADERS_FILE": str(headers_file)},
        dotenv_text="ANTHROPIC_CUSTOM_HEADERS=X-Api-Key: other-gw\n",
    )
    assert auth == ["Bearer sk-or-model"]
    assert acct.id == "openrouter:1"

    # Values present in runtime_paths.process_env still take effect as before.
    auth, acct = await _probe(
        "proc-headers",
        process_env={"ANTHROPIC_API_KEY": "sk-or-real", "ANTHROPIC_CUSTOM_HEADERS": "X-Api-Key: proc-gw"},
    )
    assert auth == ["Bearer proc-gw"]
    assert acct.id == "openrouter:1"

    auth, acct = await _probe("proc-token", process_env={"ANTHROPIC_AUTH_TOKEN": "tok-proc-env"})
    assert auth == ["Bearer tok-proc-env"]
    assert acct.id == "openrouter:1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CodexAuthError("Codex token refresh failed (HTTP 503)", status_code=503), "probe_failed"),
        (CodexAuthError("Codex token refresh failed (HTTP 429)", status_code=429), "probe_failed"),
        (CodexAuthError("Codex token refresh returned an unexpected payload."), "probe_failed"),
        (CodexAuthError("Codex auth file not found"), "not_signed_in"),
        (CodexAuthError("Codex token refresh failed (HTTP 400)", status_code=400), "not_signed_in"),
        (CodexAuthError("Codex token refresh failed (HTTP 401)", status_code=401), "not_signed_in"),
        (CodexAuthError("Codex token refresh failed (HTTP 403)", status_code=403), "not_signed_in"),
    ],
)
async def test_probe_codex_classifies_transient_vs_permanent_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: CodexAuthError,
    expected: str,
) -> None:
    """Transient refresh failures map to probe_failed while missing/refused logins map to not_signed_in."""

    def _borrow(*, codex_home: object = None) -> tuple[str, str | None]:
        _ = codex_home
        raise error

    monkeypatch.setattr(provider_probes, "borrow_codex_key", _borrow)
    assert (await provider_probes.probe_codex(None, FIXED_NOW)).unavailable_reason == expected


@pytest.mark.asyncio
async def test_codex_transient_refresh_error_preserves_last_good_reading_while_auth_refusal_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient CodexAuthError (HTTPError / 503) preserves cached windows as stale; 401 clears to not_signed_in."""
    clock = [100.0]
    mode = ["ok"]

    def _borrow(*, codex_home: object = None) -> tuple[str, str | None]:
        _ = codex_home
        if mode[0] == "ok":
            return "tok-codex", "acct-1"
        if mode[0] == "timeout":
            timeout_msg = "timed out"
            try:
                raise httpx.ReadTimeout(timeout_msg)
            except httpx.HTTPError as cause:
                msg = "Codex token refresh failed: timed out"
                raise CodexAuthError(msg) from cause
        if mode[0] == "transient":
            msg = "Codex token refresh failed (HTTP 503)"
            raise CodexAuthError(msg, status_code=503)
        msg = "Codex token refresh failed (HTTP 401)"
        raise CodexAuthError(msg, status_code=401)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(provider_probes.CODEX_USAGE_URL)
        return httpx.Response(
            200,
            json={
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 35.0,
                        "limit_window_seconds": 18000,
                        "reset_at": 1795475160,
                    },
                },
            },
        )

    _mock_httpx(monkeypatch, _handler)
    monkeypatch.setattr(provider_probes, "borrow_codex_key", _borrow)
    monkeypatch.setattr(provider_limits, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", lambda **_kw: _empty_daily_usage())
    config = Config(models={"astra": ModelConfig(provider="codex", id="gpt-5.4")})
    paths = test_runtime_paths(tmp_path)

    snap_ok = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert snap_ok.accounts[0].stale is False
    assert snap_ok.accounts[0].windows[0].used_percent == 35.0

    clock[0] += 120.0
    mode[0] = "transient"
    snap_transient = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert snap_transient.accounts[0].stale is True
    assert snap_transient.accounts[0].unavailable_reason is None
    assert snap_transient.accounts[0].windows[0].used_percent == 35.0

    clock[0] += 120.0
    mode[0] = "timeout"
    snap_timeout = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert snap_timeout.accounts[0].stale is True
    assert snap_timeout.accounts[0].unavailable_reason is None
    assert snap_timeout.accounts[0].windows[0].used_percent == 35.0

    clock[0] += 120.0
    mode[0] = "revoked"
    snap_revoked = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert snap_revoked.accounts[0].unavailable_reason == "not_signed_in"
    assert snap_revoked.accounts[0].windows == ()


def _haiku_day_report(tokens: int) -> _DailyScan:
    totals = TokenCounts(input_tokens=tokens, output_tokens=0, total_tokens=tokens)
    row = UsageDailyBreakdownRow(
        date="2026-11-23",
        totals=totals,
        run_count=1,
        model_breakdown=(UsageModelBreakdownRow("Anthropic", "claude-haiku-4-5", totals, 1),),
    )
    return _empty_daily_usage((row,))


def _acct(snap: ProviderUsageSnapshot, account_id: str) -> ProviderLimitAccount:
    return next(acct for acct in snap.accounts if acct.id == account_id)


def _setup_async_usage_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    collect_fn: Any,  # noqa: ANN401
) -> tuple[Config, Any, list[float], list[int]]:
    clock, probe_calls = [100.0], [0]

    async def _fast_probe(_cli: str | None, now: datetime) -> provider_probes.ProbeResult:
        probe_calls[0] += 1
        window = build_window(window_id="five_hour", kind="session", label="5-hour window", used_percent=25.0)
        return provider_probes.ProbeResult(
            windows=(window,),
            plan="Max 20x",
            account_label="Max 20x",
            observed_at=now,
            succeeded=True,
        )

    monkeypatch.setattr(provider_limits, "_monotonic", lambda: clock[0])
    monkeypatch.setattr(provider_limits, "probe_claude_code", _fast_probe)
    monkeypatch.setattr(provider_limits, "collect_admin_daily_usage_with_entities", collect_fn)
    config = Config(
        models={
            "opus": ModelConfig(provider="claude_code", id="claude-opus-4-6"),
            "haiku": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                pricing=ModelPricing(input=2.0, output=5.0),
            ),
        },
    )
    return config, test_runtime_paths(tmp_path), clock, probe_calls


@pytest.mark.asyncio
async def test_cold_slow_usage_scan_returns_partial_then_evicts_on_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow cold 7d scan returns live windows with partial=True, caches partial during scan, then evicts on completion."""
    scan_calls, scan_started, release_scan = [0], threading.Event(), threading.Event()

    def _slow_collect(**_kw: object) -> _DailyScan:
        scan_calls[0] += 1
        scan_started.set()
        release_scan.wait(timeout=5.0)
        return _haiku_day_report(1500)

    config, paths, _clock, probe_calls = _setup_async_usage_env(monkeypatch, tmp_path, _slow_collect)
    monkeypatch.setattr(provider_limits, "USAGE_REPORT_WAIT_SECONDS", 0.05)
    try:
        s1, s2 = await asyncio.gather(
            get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW),
            get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW),
        )
        assert s1 == s2
        assert (s1.partial, probe_calls[0]) == (True, 1)
        assert _acct(s1, "claude_code:default").windows[0].used_percent == 25.0
        assert _acct(s1, "claude_code:default").stale is False
        assert (_acct(s1, "anthropic:default").tokens_7d, _acct(s1, "anthropic:default").cost_usd_7d) == (0, None)
        assert (s1.daily_7d, s1.unattributed_tokens_7d, s1.unattributed_cost_usd_7d) == ((), 0, None)

        # Subsequent call during the scan reuses the cached partial snapshot without re-probing.
        assert await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW) == s1
        assert probe_calls[0] == 1

        inflight = list(provider_limits._USAGE_REPORT_IN_FLIGHT.get(asyncio.get_running_loop(), {}).values())
        assert len(inflight) == 1
        assert await asyncio.to_thread(scan_started.wait, 5.0)
        release_scan.set()
        await inflight[0]

        # Partial snapshot entry was evicted on completion, so the next call immediately builds partial=False.
        s4 = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
        assert (s4.partial, scan_calls[0], probe_calls[0]) == (False, 1, 2)
        assert _acct(s4, "claude_code:default").windows[0].used_percent == 25.0
        assert _acct(s4, "anthropic:default").tokens_7d == 1500
        assert _acct(s4, "anthropic:default").cost_usd_7d == pytest.approx(0.003)
        assert s4.daily_7d[0].by_account == {"anthropic:default": 1500}
    finally:
        release_scan.set()


@pytest.mark.asyncio
async def test_stale_while_revalidate_returns_cached_7d_and_refreshes_in_background(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired 7d cache serves prior totals immediately while refreshing once in the background."""
    scan_calls, refresh_started, release_refresh = [0], threading.Event(), threading.Event()

    def _collect(**_kw: object) -> _DailyScan:
        scan_calls[0] += 1
        if scan_calls[0] == 1:
            return _haiku_day_report(1500)
        refresh_started.set()
        release_refresh.wait(timeout=5.0)
        return _haiku_day_report(4200)

    config, paths, clock, _probe_calls = _setup_async_usage_env(monkeypatch, tmp_path, _collect)
    monkeypatch.setattr(provider_limits, "USAGE_REPORT_WAIT_SECONDS", 5.0)
    loop = asyncio.get_running_loop()
    try:
        s1 = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
        assert (s1.partial, _acct(s1, "anthropic:default").tokens_7d, scan_calls[0]) == (False, 1500, 1)

        clock[0] += provider_limits.USAGE_REPORT_TTL_SECONDS
        s2 = await asyncio.wait_for(
            get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW),
            timeout=1.0,
        )
        assert (s2.partial, _acct(s2, "anthropic:default").tokens_7d) == (False, 1500)
        inflight = list(provider_limits._USAGE_REPORT_IN_FLIGHT.get(loop, {}).values())
        assert len(inflight) == 1

        # force_refresh during the scan reuses the same single-flight refresh; fresh report suppresses cooldown re-scans.
        clock[0] += provider_limits.REFRESH_COOLDOWN_SECONDS
        s2_force = await asyncio.wait_for(
            get_provider_usage_snapshot(config=config, runtime_paths=paths, force_refresh=True, now=FIXED_NOW),
            timeout=1.0,
        )
        assert (s2_force.partial, _acct(s2_force, "anthropic:default").tokens_7d) == (False, 1500)
        assert list(provider_limits._USAGE_REPORT_IN_FLIGHT.get(loop, {}).values()) == inflight
        assert await asyncio.to_thread(refresh_started.wait, 5.0)
        release_refresh.set()
        await inflight[0]
        assert scan_calls[0] == 2

        alt_config = Config(models={"haiku": ModelConfig(provider="anthropic", id="claude-haiku-4-5")})
        clock[0] += provider_limits.REFRESH_COOLDOWN_SECONDS / 2
        s_fresh = await get_provider_usage_snapshot(
            config=alt_config,
            runtime_paths=paths,
            force_refresh=True,
            now=FIXED_NOW,
        )
        assert (s_fresh.accounts[0].tokens_7d, scan_calls[0]) == (4200, 2)

        # Once the snapshot cache TTL elapses (even across UTC midnight), cached daily_breakdown serves refreshed totals.
        clock[0] += provider_limits.CACHE_TTL_SECONDS
        next_utc_day = datetime(2026, 11, 24, 0, 5, 0, tzinfo=UTC)
        s3 = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=next_utc_day)
        assert (s3.partial, _acct(s3, "anthropic:default").tokens_7d, scan_calls[0]) == (False, 4200, 2)
        assert _acct(s3, "anthropic:default").cost_usd_7d == pytest.approx(0.0084)
    finally:
        release_refresh.set()


@pytest.mark.asyncio
async def test_failed_usage_scan_logs_warning_and_degrades_gracefully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed 7d scans log a secret-free warning, degrade to partial=True on cold start, and retain stale reports."""
    mode, release_fail = ["cold_fail"], threading.Event()
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))

    def _collect(**_kw: object) -> _DailyScan:
        if mode[0] == "cold_fail":
            msg = "sqlite failed secret-token-value"
            raise RuntimeError(msg)
        if mode[0] == "ok":
            return _haiku_day_report(1500)
        release_fail.wait(timeout=5.0)
        msg = "disk read failed secret-db-path"
        raise OSError(msg)

    config, paths, clock, _probe_calls = _setup_async_usage_env(monkeypatch, tmp_path, _collect)
    try:
        with capture_logs() as cold_logs:
            cold_snap = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
        assert (cold_snap.partial, _acct(cold_snap, "claude_code:default").windows[0].used_percent) == (True, 25.0)
        assert (_acct(cold_snap, "anthropic:default").tokens_7d, _acct(cold_snap, "anthropic:default").cost_usd_7d) == (
            0,
            None,
        )
        assert cold_snap.daily_7d == ()
        assert [
            (r["log_level"], r["error_type"]) for r in cold_logs if r.get("event") == "provider_usage_7d_scan_failed"
        ] == [("warning", "RuntimeError")]
        assert "secret-token-value" not in json.dumps(cold_logs)

        # Warm the 7d cache, then let a stale-while-revalidate background refresh fail without failing the snapshot.
        provider_limits.clear_cache()
        mode[0] = "ok"
        warm_snap = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
        assert _acct(warm_snap, "anthropic:default").tokens_7d == 1500

        mode[0] = "stale_fail"
        clock[0] += provider_limits.USAGE_REPORT_TTL_SECONDS
        with capture_logs() as stale_logs:
            stale_snap = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
            assert (
                stale_snap.partial,
                _acct(stale_snap, "claude_code:default").windows[0].used_percent,
                _acct(stale_snap, "anthropic:default").tokens_7d,
            ) == (False, 25.0, 1500)
            inflight = list(provider_limits._USAGE_REPORT_IN_FLIGHT.get(loop, {}).values())
            bg_done = asyncio.Event()
            assert len(inflight) == 1
            inflight.pop().add_done_callback(lambda _: bg_done.set())
            release_fail.set()
            await bg_done.wait()
            gc.collect()
            await asyncio.sleep(0)

        assert [
            (r["log_level"], r["error_type"]) for r in stale_logs if r.get("event") == "provider_usage_7d_scan_failed"
        ] == [("warning", "OSError")]
        assert "secret-db-path" not in json.dumps(stale_logs)
        assert loop_errors == []
        (retained_daily, _), _ = next(iter(provider_limits._USAGE_REPORT_CACHE.values()))
        assert retained_daily[0].totals.total_tokens == 1500
    finally:
        release_fail.set()
        loop.set_exception_handler(prev_handler)


def _sum_mixes(mixes: Any) -> TokenMix:  # noqa: ANN401
    total = TokenMix()
    for mix in mixes:
        assert mix is not None
        total = total.add(mix)
    return total


def _usage_run(
    run_id: str,
    provider: str,
    model: str,
    created_at: float,
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        user_id="@alice:example.org",
        model_provider=provider,
        model=model,
        created_at=created_at,
        metrics=RunMetrics(
            tokens=TokenCounts(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                total_tokens=total_tokens,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_rich_7d_analytics_token_mix_normalization_cache_savings_and_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end 7d analytics reconcile accounts, daily costs, models, agents, uncached input, and net cache savings."""
    monkeypatch.setattr(
        provider_limits,
        "probe_codex",
        AsyncMock(return_value=provider_probes.ProbeResult(unavailable_reason="not_signed_in")),
    )
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={"code": AgentConfig(display_name="Code"), "research": AgentConfig(display_name="Research")},
        models={
            "haiku": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                pricing=ModelPricing(input=2.0, output=6.0, cache_read=0.2, cache_write=2.5),
            ),
            "gpt": ModelConfig(
                provider="openai",
                id="gpt-5.4",
                pricing=ModelPricing(input=5.0, output=15.0, cache_read=1.0),
            ),
            "codex_a": ModelConfig(
                provider="codex",
                id="gpt-5.4",
                pricing=ModelPricing(input=4.0, output=12.0, cache_read=0.5),
                extra_kwargs={"codex_home": str(tmp_path / "c1")},
            ),
            "codex_b": ModelConfig(provider="codex", id="gpt-5.4", extra_kwargs={"codex_home": str(tmp_path / "c2")}),
            "flash": ModelConfig(provider="google", id="gemini-2.5-flash"),
        },
    )
    old_ts = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()
    day1_ts = datetime(2026, 11, 18, 9, 0, tzinfo=UTC).timestamp()
    day2_ts = datetime(2026, 11, 23, 14, 0, tzinfo=UTC).timestamp()

    # Long-lived "code" session updated today mixes an out-of-window Nov 10 run with in-window Anthropic & OpenAI runs.
    persist_runs(
        create_agent_state_storage("code", paths.storage_root / "agents" / "code"),
        ConversationSession(kind="agent", session_id="long-lived", owner_id="code"),
        # Anthropic parser records total_tokens = input + output (cache read/write sit outside input/total).
        _usage_run(
            "run-old",
            "Anthropic",
            "claude-haiku-4-5",
            old_ts,
            input_tokens=90_000,
            output_tokens=10_000,
            cache_read=500_000,
            total_tokens=100_000,
        ),
        # Day 1 Anthropic net cache savings: 400k*(2.0-0.2) - 200k*(2.5-2.0) = $0.72 - $0.10 = $0.62.
        _usage_run(
            "run-ant-1",
            "Anthropic",
            "claude-haiku-4-5",
            day1_ts,
            input_tokens=100_000,
            output_tokens=50_000,
            cache_read=400_000,
            cache_write=200_000,
            total_tokens=150_000,
        ),
        # OpenAI includes cache inside input -> uncached = 500k - 300k = 200k, total = input + output = 600k.
        _usage_run(
            "run-oai-1",
            "OpenAI",
            "gpt-5.4",
            day2_ts,
            input_tokens=500_000,
            output_tokens=100_000,
            cache_read=300_000,
            total_tokens=600_000,
        ),
    )
    # "research" mixes ambiguous priced Codex, priced Anthropic, and unpriced Google (cache inside input, no savings).
    persist_runs(
        create_agent_state_storage("research", paths.storage_root / "agents" / "research"),
        ConversationSession(kind="agent", session_id="res-1", owner_id="research"),
        _usage_run(
            "run-cdx-1",
            "OpenAI Codex",
            "gpt-5.4",
            day1_ts,
            input_tokens=300_000,
            output_tokens=50_000,
            cache_read=200_000,
            total_tokens=350_000,
        ),
        _usage_run(
            "run-ant-2",
            "Anthropic",
            "claude-haiku-4-5",
            day2_ts,
            input_tokens=50_000,
            output_tokens=25_000,
            cache_read=100_000,
            total_tokens=75_000,
        ),
        _usage_run(
            "run-goog-1",
            "Google",
            "gemini-2.5-flash",
            day2_ts,
            input_tokens=80_000,
            output_tokens=20_000,
            cache_read=60_000,
            total_tokens=100_000,
        ),
    )

    snapshot = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    assert snapshot.partial is False
    by_id = {acct.id: acct for acct in snapshot.accounts}
    # Anthropic tokens_7d == input + output (225k), while token_mix_7d captures all 4 processed buckets (925k total).
    assert (by_id["anthropic:default"].tokens_7d, by_id["anthropic:default"].cost_usd_7d) == (
        225_000,
        pytest.approx(1.35),
    )
    assert by_id["anthropic:default"].token_mix_7d == TokenMix(150_000, 75_000, 500_000, 200_000)
    assert (by_id["openai:default"].tokens_7d, by_id["openai:default"].cost_usd_7d) == (600_000, pytest.approx(2.8))
    assert by_id["openai:default"].token_mix_7d == TokenMix(200_000, 100_000, 300_000, 0)
    assert (by_id["google:default"].tokens_7d, by_id["google:default"].cost_usd_7d) == (100_000, None)
    assert by_id["google:default"].token_mix_7d == TokenMix(20_000, 20_000, 60_000, 0)
    assert (by_id["codex:default"].token_mix_7d, by_id["codex:2"].token_mix_7d) == (None, None)
    assert (snapshot.unattributed_tokens_7d, snapshot.unattributed_cost_usd_7d) == (350_000, pytest.approx(1.1))

    assert [row.day for row in snapshot.daily_7d] == ["2026-11-18", "2026-11-23"]
    assert (snapshot.daily_7d[0].cost_by_account, snapshot.daily_7d[0].unattributed_cost_usd) == (
        {"anthropic:default": 1.08},
        pytest.approx(1.1),
    )
    assert (snapshot.daily_7d[1].cost_by_account, snapshot.daily_7d[1].unattributed_cost_usd) == (
        {"anthropic:default": 0.27, "openai:default": 2.8},
        None,
    )

    expected_mix = TokenMix(470_000, 245_000, 1_060_000, 200_000)
    assert snapshot.token_mix_7d == expected_mix
    # Net savings: Anthropic ($0.62 + $0.18 = $0.80, after -$0.10 write premium) + OpenAI $1.20 + Codex $0.70 = $2.70.
    assert snapshot.cache_savings_usd_7d == pytest.approx(2.7)

    assert [
        (m.provider, m.model, m.account_id, m.tokens_7d, m.cost_usd_7d, m.run_count, m.daily_tokens_7d)
        for m in snapshot.top_models_7d
    ] == [
        ("OpenAI", "gpt-5.4", "openai:default", 600_000, 2.8, 1, (0, 600_000)),
        ("Anthropic", "claude-haiku-4-5", "anthropic:default", 225_000, 1.35, 2, (150_000, 75_000)),
        ("OpenAI Codex", "gpt-5.4", None, 350_000, 1.1, 1, (350_000, 0)),
        ("Google", "gemini-2.5-flash", "google:default", 100_000, None, 1, (0, 100_000)),
    ]
    assert "account_id" not in snapshot.top_models_7d[2].to_dict()
    assert [(a.entity, a.tokens_7d, a.cost_usd_7d, a.run_count, a.daily_tokens_7d) for a in snapshot.top_agents_7d] == [
        ("code", 750_000, 3.88, 2, (150_000, 600_000)),
        ("research", 525_000, 1.37, 3, (350_000, 175_000)),
    ]

    total_tokens = sum(a.tokens_7d for a in snapshot.accounts) + snapshot.unattributed_tokens_7d
    assert (
        total_tokens
        == sum(m.tokens_7d for m in snapshot.top_models_7d)
        == sum(a.tokens_7d for a in snapshot.top_agents_7d)
        == 1_275_000
    )
    assert _sum_mixes(m.token_mix_7d for m in snapshot.top_models_7d) == expected_mix
    assert _sum_mixes(a.token_mix_7d for a in snapshot.top_agents_7d) == expected_mix


@pytest.mark.asyncio
async def test_net_cache_savings_subtracts_cache_write_premium_and_omits_non_positive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Net cache savings deducts cache-write surcharges ($0.62 vs $0.72 gross) and omits <= 0 net savings."""
    haiku_cfg = Config(
        models={
            "haiku": ModelConfig(
                provider="anthropic",
                id="claude-haiku-4-5",
                pricing=ModelPricing(input=2.0, output=6.0, cache_read=0.2, cache_write=2.5),
            ),
        },
    )
    paths = test_runtime_paths(tmp_path)

    # Gross read discount = 400k*(2.0-0.2) = $0.72; write premium = 200k*(2.5-2.0) = $0.10 -> net = $0.62.
    net_positive = (
        UsageDailyBreakdownRow(
            "2026-11-23",
            TokenCounts(
                input_tokens=100_000,
                output_tokens=50_000,
                cache_read_tokens=400_000,
                cache_write_tokens=200_000,
                total_tokens=150_000,
            ),
            1,
            (
                UsageModelBreakdownRow(
                    "Anthropic",
                    "claude-haiku-4-5",
                    TokenCounts(
                        input_tokens=100_000,
                        output_tokens=50_000,
                        cache_read_tokens=400_000,
                        cache_write_tokens=200_000,
                        total_tokens=150_000,
                    ),
                    1,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage(net_positive),
    )
    pos_snap = await get_provider_usage_snapshot(config=haiku_cfg, runtime_paths=paths, now=FIXED_NOW)
    assert pos_snap.cache_savings_usd_7d == pytest.approx(0.62)

    # Write-heavy workload: read discount 50k*1.8=$0.09 < write premium 400k*0.5=$0.20 -> net -$0.11 -> omitted.
    provider_limits.clear_cache()
    net_negative = (
        UsageDailyBreakdownRow(
            "2026-11-23",
            TokenCounts(
                input_tokens=100_000,
                output_tokens=20_000,
                cache_read_tokens=50_000,
                cache_write_tokens=400_000,
                total_tokens=120_000,
            ),
            1,
            (
                UsageModelBreakdownRow(
                    "Anthropic",
                    "claude-haiku-4-5",
                    TokenCounts(
                        input_tokens=100_000,
                        output_tokens=20_000,
                        cache_read_tokens=50_000,
                        cache_write_tokens=400_000,
                        total_tokens=120_000,
                    ),
                    1,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage(net_negative),
    )
    neg_snap = await get_provider_usage_snapshot(config=haiku_cfg, runtime_paths=paths, now=FIXED_NOW)
    assert neg_snap.cache_savings_usd_7d is None
    assert "cache_savings_usd_7d" not in neg_snap.to_dict()


@pytest.mark.asyncio
async def test_rich_analytics_omission_rules_vertexai_legacy_provider_and_top_12_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero mixes/savings omit cleanly, vertexai_claude keeps input uncached, legacy names merge, and lists cap at 12."""
    model_rows = [
        UsageModelBreakdownRow(
            "VertexAI",
            "custom-endpoint",
            TokenCounts(input_tokens=400, output_tokens=100, cache_read_tokens=1000, total_tokens=500),
            1,
        ),
        UsageModelBreakdownRow(
            "OpenAI Chat",
            "gpt-5.4",
            TokenCounts(input_tokens=300, output_tokens=100, cache_read_tokens=200, total_tokens=400),
            2,
        ),
        UsageModelBreakdownRow(
            "OpenAI",
            "gpt-5.4",
            TokenCounts(input_tokens=500, output_tokens=100, cache_read_tokens=200, total_tokens=600),
            3,
        ),
        *[
            UsageModelBreakdownRow(
                "Google",
                f"gemini-m-{i:02d}",
                TokenCounts(input_tokens=100 + i, output_tokens=50, total_tokens=150 + i),
                1,
            )
            for i in range(15)
        ],
    ]
    daily = (
        UsageDailyBreakdownRow(
            "2026-11-23",
            TokenCounts(total_tokens=sum(r.totals.total_tokens for r in model_rows)),
            len(model_rows),
            tuple(model_rows),
        ),
    )
    by_entity = {
        "agent-infra": (
            UsageDailyBreakdownRow(
                "2026-11-23",
                TokenCounts(total_tokens=sum(r.totals.total_tokens for r in model_rows[:3])),
                sum(r.run_count for r in model_rows[:3]),
                tuple(model_rows[:3]),
            ),
        ),
        **{
            f"agent-{i:02d}": (
                UsageDailyBreakdownRow(
                    "2026-11-23",
                    TokenCounts(input_tokens=100 + i, output_tokens=50, total_tokens=150 + i),
                    1,
                    (model_rows[3 + i],),
                ),
            )
            for i in range(15)
        },
    }
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage(daily, by_entity),
    )
    config = Config(
        models={
            "v_claude": ModelConfig(provider="vertexai_claude", id="custom-endpoint"),
            "oai_a": ModelConfig(provider="openai", id="gpt-5.4", pricing=ModelPricing(input=2.0, output=4.0)),
        },
    )
    snapshot = await get_provider_usage_snapshot(
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        now=FIXED_NOW,
    )
    assert snapshot.cache_savings_usd_7d is None
    assert "cache_savings_usd_7d" not in snapshot.to_dict()
    assert _acct(snapshot, "vertexai_claude:default").tokens_7d == 500
    assert _acct(snapshot, "vertexai_claude:default").token_mix_7d == TokenMix(400, 100, 1000, 0)
    assert (
        snapshot.top_models_7d[0].provider,
        snapshot.top_models_7d[0].model,
        snapshot.top_models_7d[0].run_count,
        snapshot.top_models_7d[0].tokens_7d,
    ) == ("OpenAI", "gpt-5.4", 5, 1000)
    assert snapshot.top_models_7d[0].token_mix_7d == TokenMix(400, 200, 400, 0)
    assert (len(snapshot.top_models_7d), len(snapshot.top_agents_7d)) == (12, 12)
    assert [a.entity for a in snapshot.top_agents_7d[:4]] == ["agent-infra", "agent-14", "agent-13", "agent-12"]

    empty_wire = ProviderUsageSnapshot(
        checked_at="2026-11-23T20:00:00Z",
        partial=False,
        accounts=(
            ProviderLimitAccount(
                "anthropic:default",
                "anthropic",
                "Anthropic",
                None,
                None,
                (),
                (),
                None,
                "2026-11-23T20:00:00Z",
                False,
                0,
                None,
                False,
                TokenMix(),
            ),
        ),
        daily_7d=(DailyAccountUsage("2026-11-23", {}, 0),),
        unattributed_tokens_7d=0,
        unattributed_cost_usd_7d=None,
        token_mix_7d=TokenMix(),
        cache_savings_usd_7d=0.0,
    ).to_dict()
    assert not ({"token_mix_7d", "cache_savings_usd_7d", "top_models_7d", "top_agents_7d"} & empty_wire.keys())
    assert "token_mix_7d" not in empty_wire["accounts"][0]
    assert not ({"cost_by_account", "unattributed_cost_usd"} & empty_wire["daily_7d"][0].keys())


def test_fit_to_device_budget_progressive_shedding_order() -> None:
    """fit_to_device_budget sheds optional rich analytics without setting partial=True, then drops daily_7d & trims keys."""
    mix = {"input_tokens": 1000, "output_tokens": 500, "cache_read_tokens": 3000, "cache_write_tokens": 500}
    rich_payload: dict[str, object] = {
        "version": 1,
        "request_id": "req-rich",
        "checked_at": "2026-11-23T20:00:00Z",
        "partial": False,
        "accounts": [
            {
                "id": "anthropic:default",
                "provider": "anthropic",
                "display_name": "Anthropic",
                "model_keys": [f"model-{i:03d}" for i in range(40)],
                "windows": [],
                "stale": False,
                "tokens_7d": 5000,
                "cost_usd_7d": 1.25,
                "cost_estimated": True,
                "token_mix_7d": dict(mix),
            },
        ],
        "daily_7d": [
            {
                "day": f"2026-11-{d:02d}",
                "by_account": {"anthropic:default": 1000 * d},
                "unattributed_tokens": 50,
                "cost_by_account": {"anthropic:default": round(0.2 * d, 4)},
                "unattributed_cost_usd": 0.05,
            }
            for d in range(17, 24)
        ],
        "unattributed_tokens_7d": 350,
        "unattributed_cost_usd_7d": 0.35,
        "token_mix_7d": dict(mix),
        "cache_savings_usd_7d": 0.42,
        "top_models_7d": [
            {
                "provider": "Anthropic",
                "model": f"claude-m-{i}",
                "account_id": "anthropic:default",
                "tokens_7d": 1000,
                "cost_usd_7d": 1.0,
                "run_count": 5,
                "daily_tokens_7d": [100] * 7,
            }
            for i in range(8)
        ],
        "top_agents_7d": [
            {
                "entity": f"agent-{i}",
                "tokens_7d": 1000,
                "cost_usd_7d": 1.0,
                "run_count": 5,
                "daily_tokens_7d": [100] * 7,
            }
            for i in range(8)
        ],
    }

    full_size = len(json.dumps(rich_payload).encode("utf-8"))
    unmodified = fit_to_device_budget(rich_payload, max_bytes=full_size)
    assert (unmodified == rich_payload, unmodified["partial"]) == (True, False)

    # Stage 1: strips optional rich analytics while preserving daily_7d, model_keys, and partial=False.
    stage1 = fit_to_device_budget(rich_payload, max_bytes=full_size - 100)
    assert stage1["partial"] is False
    assert len(json.dumps(stage1).encode("utf-8")) <= full_size - 100
    assert not ({"top_agents_7d", "top_models_7d", "token_mix_7d", "cache_savings_usd_7d"} & stage1.keys())
    assert "token_mix_7d" not in stage1["accounts"][0]
    assert len(stage1["daily_7d"]) == 7
    assert all("cost_by_account" not in row and "unattributed_cost_usd" not in row for row in stage1["daily_7d"])
    assert (stage1["daily_7d"][0]["by_account"], len(stage1["accounts"][0]["model_keys"])) == (
        {"anthropic:default": 17000},
        40,
    )

    # Stage 2: drops daily_7d and flips partial=True while preserving all model_keys.
    stage1_size = len(json.dumps(stage1).encode("utf-8"))
    stage2 = fit_to_device_budget(rich_payload, max_bytes=stage1_size - 50)
    assert (stage2["partial"], stage2["daily_7d"], len(stage2["accounts"][0]["model_keys"])) == (True, [], 40)

    # Stage 3: trims model_keys with partial=True.
    stage2_size = len(json.dumps(stage2).encode("utf-8"))
    stage3 = fit_to_device_budget(rich_payload, max_bytes=stage2_size - 100)
    assert stage3["partial"] is True
    assert stage3["daily_7d"] == []
    assert len(stage3["accounts"][0]["model_keys"]) < 40


@pytest.mark.asyncio
async def test_single_pass_clamped_cache_read_reconciles_accounts_models_and_agents(
    tmp_path: Path,
) -> None:
    """Clamping cache_read > input_tokens happens once per entity row so accounts, models, daily, and agents agree."""
    paths = test_runtime_paths(tmp_path)
    config = Config(
        agents={"agent_a": AgentConfig(display_name="Agent A"), "agent_b": AgentConfig(display_name="Agent B")},
        models={
            "gpt": ModelConfig(
                provider="openai",
                id="gpt-5.4",
                pricing=ModelPricing(input=5.0, output=15.0, cache_read=1.0),
            ),
        },
    )
    day_ts = datetime(2026, 11, 23, 12, 0, tzinfo=UTC).timestamp()

    # agent_a reports cache_read > input_tokens (clamped uncached=0; cost = 0*5 + 150k*1 + 10k*15 = $0.30).
    persist_runs(
        create_agent_state_storage("agent_a", paths.storage_root / "agents" / "agent_a"),
        ConversationSession(kind="agent", session_id="s-a", owner_id="agent_a"),
        _usage_run(
            "run-a",
            "OpenAI",
            "gpt-5.4",
            day_ts,
            input_tokens=100_000,
            output_tokens=10_000,
            cache_read=150_000,
            total_tokens=110_000,
        ),
    )
    # agent_b reports cache_read=0 (uncached=100k; cost = 100k*5 + 0 + 10k*15 = $0.65).
    persist_runs(
        create_agent_state_storage("agent_b", paths.storage_root / "agents" / "agent_b"),
        ConversationSession(kind="agent", session_id="s-b", owner_id="agent_b"),
        _usage_run(
            "run-b",
            "OpenAI",
            "gpt-5.4",
            day_ts,
            input_tokens=100_000,
            output_tokens=10_000,
            cache_read=0,
            total_tokens=110_000,
        ),
    )

    snapshot = await get_provider_usage_snapshot(config=config, runtime_paths=paths, now=FIXED_NOW)
    agents = {agent.entity: agent for agent in snapshot.top_agents_7d}
    assert agents["agent_a"].token_mix_7d == TokenMix(0, 10_000, 150_000, 0)
    assert agents["agent_a"].cost_usd_7d == pytest.approx(0.3)
    assert agents["agent_b"].token_mix_7d == TokenMix(100_000, 10_000, 0, 0)
    assert agents["agent_b"].cost_usd_7d == pytest.approx(0.65)

    # Account, daily row, model rollup, and top-level token_mix_7d all match agent_a + agent_b exactly ($0.95, 100k uncached).
    expected_mix = TokenMix(100_000, 20_000, 150_000, 0)
    openai_acct = _acct(snapshot, "openai:default")
    assert openai_acct.token_mix_7d == expected_mix
    assert openai_acct.cost_usd_7d == pytest.approx(0.95)
    assert snapshot.daily_7d[0].cost_by_account == {"openai:default": pytest.approx(0.95)}
    assert snapshot.top_models_7d[0].token_mix_7d == expected_mix
    assert snapshot.top_models_7d[0].cost_usd_7d == pytest.approx(0.95)
    assert snapshot.token_mix_7d == expected_mix


@pytest.mark.asyncio
async def test_vertexai_claude_hyphenated_provider_matches_priced_and_unpriced_token_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hyphenated vertexai-claude produces identical uncached input counts with and without pricing configured."""
    row = UsageDailyBreakdownRow(
        "2026-11-23",
        TokenCounts(input_tokens=400_000, output_tokens=100_000, cache_read_tokens=900_000, total_tokens=500_000),
        1,
        (
            UsageModelBreakdownRow(
                "VertexAI",
                "custom-endpoint",
                TokenCounts(
                    input_tokens=400_000,
                    output_tokens=100_000,
                    cache_read_tokens=900_000,
                    total_tokens=500_000,
                ),
                1,
            ),
        ),
    )
    monkeypatch.setattr(
        provider_limits,
        "collect_admin_daily_usage_with_entities",
        lambda **_kw: _empty_daily_usage((row,), {"code": (row,)}),
    )
    paths = test_runtime_paths(tmp_path)
    expected_mix = TokenMix(400_000, 100_000, 900_000, 0)

    unpriced_cfg = Config(models={"vc": ModelConfig(provider="vertexai-claude", id="custom-endpoint")})
    priced_cfg = Config(
        models={
            "vc": ModelConfig(
                provider="vertexai-claude",
                id="custom-endpoint",
                pricing=ModelPricing(input=3.0, output=15.0, cache_read=0.3),
            ),
        },
    )
    underscored_cfg = Config(
        models={
            "vc": ModelConfig(
                provider="vertexai_claude",
                id="custom-endpoint",
                pricing=ModelPricing(input=3.0, output=15.0, cache_read=0.3),
            ),
        },
    )

    unpriced_snap = await get_provider_usage_snapshot(config=unpriced_cfg, runtime_paths=paths, now=FIXED_NOW)
    priced_snap = await get_provider_usage_snapshot(config=priced_cfg, runtime_paths=paths, now=FIXED_NOW)
    underscored_snap = await get_provider_usage_snapshot(config=underscored_cfg, runtime_paths=paths, now=FIXED_NOW)

    for snap in (unpriced_snap, priced_snap, underscored_snap):
        assert snap.token_mix_7d == expected_mix
        assert _acct(snap, "vertexai_claude:default").token_mix_7d == expected_mix
        assert snap.top_models_7d[0].token_mix_7d == expected_mix
        assert snap.top_agents_7d[0].token_mix_7d == expected_mix
    # Priced cost uses full 400k uncached input (400k*3 + 900k*0.3 + 100k*15 = 1.2 + 0.27 + 1.5 = $2.97).
    assert _acct(priced_snap, "vertexai_claude:default").cost_usd_7d == pytest.approx(2.97)
    assert _acct(underscored_snap, "vertexai_claude:default").cost_usd_7d == pytest.approx(2.97)
