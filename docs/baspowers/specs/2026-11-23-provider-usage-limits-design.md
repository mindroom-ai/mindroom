# Provider Usage Limits & Per-Account Token Usage — Design Spec (MindRoom Backend + Dashboard)

**Date:** 2026-11-23
**Status:** Approved by Bas (Hybrid A + B, with OpenRouter & MindRoom Dashboard)

## Goal

Expose per-account provider usage limits (remaining capacity windows, reset times, plan/cap details) and 7-day token/cost aggregates for configured MindRoom providers (`claude_code`, `codex`, `openrouter`, and pay-as-you-go API-key providers) via:
1. Encrypted Matrix to-device RPC (`io.mindroom.usage.request` / `io.mindroom.usage.response`) for MindRoom Chat.
2. Admin Dashboard REST endpoint (`GET /api/usage/provider-limits`) and a **Provider Limits** section on the Dashboard's Usage tab (`frontend/src/components/Usage/`).

## Constraints & Security

- **Admin-only visibility:** Both Matrix to-device RPC and REST endpoints authorize exclusively via `is_platform_administrator(user_id, config, runtime_paths)` (or `Depends(verify_user)` on REST). Non-admin Matrix to-device requests are silently dropped before any probe or store read, and admin status + pinned Matrix device are rechecked before sending the encrypted reply (matching `model_catalog_receiver.py`).
- **No secret or path leakage:** Account IDs are opaque slugs (`claude_code:default`, `codex:default`, `codex:2`, `openrouter:1`, `anthropic:default`). Never emit raw API keys, key hashes, filesystem paths (`codex_home`), or raw tokens.
- **Official endpoints only:** Only send OpenRouter key probes (`GET https://openrouter.ai/api/v1/key`) for credentials routed to official `openrouter.ai` hosts (including `anthropic` / `openai` provider entries whose effective `base_url` host is `openrouter.ai`). Never send custom-gateway keys to OpenRouter.
- **Claude Code probe isolation:** Reuse `providers/claude_code/launcher.py` (env var scrubbing) with `--tools ""`, `--setting-sources ""`, `--strict-mcp-config`, `--no-session-persistence`, `--permission-mode dontAsk`, empty temp `cwd`, no user turn sent, verified first-party subscription on `initialize`, and process reaped on a 10s timeout. Never read `~/.claude*` or keychain directly.
- **Tach boundaries:** Passive limit capture lives in a dependency-free leaf module `src/mindroom/usage/limit_store.py`. Provider adapters import ONLY `limit_store`. Dashboard/API startup never imports optional `claude_agent_sdk`.
- **64 KB Olm cap:** Serialized to-device response must stay ≤ 48,000 UTF-8 bytes; if exceeded, drop `daily_7d` first and set `partial: true`.

## Architecture & Module Map

1. **`src/mindroom/usage/limit_store.py`** (leaf, stdlib-only):
   - Thread/async-safe in-memory store for passively observed rate-limit windows keyed by `(provider, scope_key)`:
     - `record_codex_headers(scope_key: str, headers: Mapping[str, str], *, now: datetime | None = None) -> None`: parses `x-codex-primary-used-percent`, `x-codex-primary-window-minutes`, `x-codex-primary-reset-at` and `x-codex-secondary-*` headers.
     - `record_claude_rate_limit(scope_key: str, *, rate_limit_type: str | None, utilization: float | None, resets_at: int | None, now: datetime | None = None) -> None`: converts 0..1 `utilization` to 0..100 `used_percent` for `five_hour` (300m) / `seven_day` (10080m) / model-scoped buckets.
     - `get_passive_windows(provider: str, scope_key: str) -> PassiveLimitReading | None` and `clear()` (for tests).
2. **Passive provider hooks** (minimal, non-failing):
   - `src/mindroom/providers/openai/responses.py`: on `route_key == "codex"` (or Codex responses), inspect `sdk_stream.response.headers` and call `record_codex_headers(...)` (never fails a turn on malformed headers).
   - `src/mindroom/providers/claude_code/turn.py` & `session.py`: on every `RateLimitEvent` (both active turn and parked drain), call `record_claude_rate_limit(...)` in addition to existing `rejected` handling.
3. **`src/mindroom/usage/provider_limits.py`** (probes + aggregation + cache):
   - Resolves distinct billing accounts from `config.models` + `runtime_paths`:
     - `codex`: grouped by effective `codex_home` (including `CODEX_HOME` env var); probes `GET https://chatgpt.com/backend-api/wham/usage` with `borrow_codex_key(codex_home=...)` + `ChatGPT-Account-Id` header when present; merges/falls back to passive `limit_store` reading.
     - `claude_code`: single host CLI login (or distinct `cli_path`); probes via isolated `launcher.py` subprocess sending `initialize` + `get_usage` control requests over stream-json; merges/falls back to passive `limit_store` reading.
     - `openrouter`: models whose effective route hits official `openrouter.ai` (including `provider: openrouter`, or `anthropic`/`openai` with `base_url` host `openrouter.ai`), deduplicated in-memory by credential digest; probes `GET https://openrouter.ai/api/v1/key`. If `limit` is set, emits `credit_limit` window (`detail: "$X.XX / $Y.YY left"`); if `limit` is `None`, emits `windows: []`, `plan: "Unlimited credit"` (never 100% remaining).
     - Other configured providers (`anthropic`, `openai`, `google`/`gemini`, etc.): pay-as-you-go accounts with `windows: []`, `plan: "Pay-as-you-go"`.
   - 7-day token & cost attribution:
     - Uses `collect_admin_usage(config=config, runtime_paths=runtime_paths, include_daily=True)` (reusing cached report where valid) filtered to the last 7 UTC calendar dates (`YYYY-MM-DD`).
     - Maps `(provider, model)` rows to the account owning that model. If multiple accounts serve the same `(provider, model)`, places those tokens in `unattributed_tokens_7d` / `daily_7d[].unattributed_tokens` rather than double-counting or fabricating a split.
     - Estimates USD cost using `mindroom.budgets.pricing` when model prices exist; leaves unpriced cost `None` and sets `cost_estimated: True` when priced.
   - Caching: 60s TTL cache per `(runtime_paths, config_generation)`, single-flight deduplication (`asyncio.Lock` / shared task), keep-last-good on probe failure (`stale: True`), `force_refresh` subject to 10s cooldown.
4. **`src/mindroom/usage/limits_receiver.py`** (encrypted Matrix to-device RPC):
   - Mirrors `src/mindroom/model_catalog_receiver.py`:
     - Handles `io.mindroom.usage.request`: `{version: 1, request_id: str, room_id: str, force_refresh?: bool}`.
     - Checks `answers_in_room(room_id)` for single-responder election (room is NEVER used for authorization).
     - Checks `is_platform_administrator(event.sender, config, runtime_paths)` before any work; silently drops non-admins.
     - Bounded concurrency & per-user rate limiting; rechecks admin + `authenticated_sender_is_current` before sending encrypted `io.mindroom.usage.response`.
     - Enforces ≤ 48,000 UTF-8 bytes payload cap (drops `daily_7d`, then trims `model_keys`, sets `partial: True`).
   - Wired in `src/mindroom/bot.py` alongside `register_model_catalog_receiver`.
5. **Dashboard REST API & UI**:
   - `src/mindroom/api/usage.py`: `GET /api/usage/provider-limits?refresh=false` (`Depends(verify_user)`, `Cache-Control: no-store`).
   - `frontend/src/types/usage.ts`, `frontend/src/services/usageService.ts`: types and `fetchProviderLimits(refresh?: boolean)`.
   - `frontend/src/components/Usage/ProviderLimits.tsx` + wired into `Usage.tsx`: shows per-account cards with remaining % bars, T3-style pace tick marks, reset countdowns, stale/unavailable notices, and a Refresh button above "All-time recorded usage".

## Wire Contract (`version: 1`)

Shared fixture path: `tests/fixtures/provider_usage_contract.json` (mirrored to `mindroom-chat` at `src/app/usage/__fixtures__/providerUsageContract.json`).

- Window fields: `id: str`, `kind: "session" | "weekly" | "monthly"`, `label: str`, `used_percent: float`, `remaining_percent: float`, `window_duration_mins?: int`, `resets_at?: str` (ISO-8601 UTC), `detail?: str`.
- Account fields: `id: str`, `provider: str`, `display_name: str`, `account_label?: str`, `plan?: str`, `model_keys: list[str]`, `windows: list[Window]`, `unavailable_reason?: "unsupported" | "not_signed_in" | "probe_failed"`, `checked_at?: str`, `stale: bool`, `tokens_7d: int`, `cost_usd_7d: float | None`, `cost_estimated: bool`.
- Daily row fields: `day: str` (`YYYY-MM-DD`), `by_account: dict[str, int]`, `unattributed_tokens: int`.
- Snapshot fields: `version: 1`, `request_id?: str` (to-device only), `checked_at: str`, `partial: bool`, `accounts: list[Account]`, `daily_7d: list[DailyRow]`, `unattributed_tokens_7d: int`, `unattributed_cost_usd_7d: float | None`.
