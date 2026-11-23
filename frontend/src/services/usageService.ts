import { isPlainObject } from "@/lib/configSchema";
import type {
  ProviderLimitAccount,
  ProviderLimitWindow,
  ProviderUsageDailyRow,
  ProviderUsageSnapshot,
  TokenTotals,
  UsagePollResult,
  UsageReport,
} from "@/types/usage";

const DEFAULT_RETRY_AFTER_MS = 5_000;
const MIN_RETRY_AFTER_MS = 1_000;
const MAX_RETRY_AFTER_MS = 30_000;
const INVALID_RESPONSE_MESSAGE =
  "Could not read usage data from the server. Try again.";

const TOKEN_FIELDS = [
  "input_tokens",
  "output_tokens",
  "total_tokens",
  "cache_read_tokens",
  "cache_write_tokens",
  "reasoning_tokens",
  "audio_input_tokens",
  "audio_output_tokens",
  "audio_total_tokens",
] as const satisfies readonly (keyof TokenTotals)[];

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isTokenTotals(value: unknown): value is TokenTotals {
  return (
    isPlainObject(value) && TOKEN_FIELDS.every((field) => isCount(value[field]))
  );
}

function isCoverage(value: unknown): boolean {
  return (
    isPlainObject(value) &&
    isCount(value.scanned_sources) &&
    isCount(value.unavailable_sources) &&
    typeof value.note === "string"
  );
}

function isUsageRow(
  value: unknown,
  count: "session_count" | "run_count",
): value is Record<string, unknown> {
  return (
    isPlainObject(value) && isTokenTotals(value.totals) && isCount(value[count])
  );
}

function isModelRow(
  value: unknown,
  count: "session_count" | "run_count",
): boolean {
  return (
    isUsageRow(value, count) &&
    typeof value.provider === "string" &&
    typeof value.model === "string"
  );
}

function isDay(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(value)) &&
    new Date(value).toISOString().slice(0, 10) === value
  );
}

function isDailyRow(value: unknown): boolean {
  return (
    isUsageRow(value, "run_count") &&
    isDay(value.date) &&
    Array.isArray(value.model_breakdown) &&
    value.model_breakdown.every((row) => isModelRow(row, "run_count"))
  );
}

function isRequesterRow(value: unknown): boolean {
  return (
    isUsageRow(value, "run_count") &&
    (value.user_id === null || typeof value.user_id === "string") &&
    Array.isArray(value.model_breakdown) &&
    value.model_breakdown.every((row) => isModelRow(row, "run_count")) &&
    Array.isArray(value.daily_breakdown) &&
    value.daily_breakdown.every(isDailyRow)
  );
}

function isEntityRow(value: unknown): boolean {
  return (
    isUsageRow(value, "session_count") &&
    value.dimension === "entity" &&
    typeof value.key === "string" &&
    isCount(value.run_count) &&
    Array.isArray(value.cumulative_model_breakdown) &&
    value.cumulative_model_breakdown.every((row) =>
      isModelRow(row, "session_count"),
    ) &&
    isTokenTotals(value.retained_run_totals) &&
    Array.isArray(value.user_breakdown) &&
    value.user_breakdown.every(isRequesterRow)
  );
}

function isUsageReport(value: unknown): value is UsageReport {
  if (!isPlainObject(value)) return false;

  return (
    value.schema_version === 1 &&
    value.scope === "admin" &&
    typeof value.generated_at === "string" &&
    isDay(value.generated_at.slice(0, 10)) &&
    Number.isFinite(Date.parse(value.generated_at)) &&
    isCount(value.session_count) &&
    isTokenTotals(value.totals) &&
    Array.isArray(value.breakdown) &&
    value.breakdown.every(isEntityRow) &&
    isCoverage(value.coverage) &&
    Array.isArray(value.cumulative_model_breakdown) &&
    value.cumulative_model_breakdown.every((row) =>
      isModelRow(row, "session_count"),
    ) &&
    isCoverage(value.cumulative_model_coverage) &&
    Array.isArray(value.user_breakdown) &&
    value.user_breakdown.every(isRequesterRow) &&
    isCoverage(value.user_coverage) &&
    Array.isArray(value.daily_breakdown) &&
    value.daily_breakdown.every(isDailyRow) &&
    isCoverage(value.daily_coverage)
  );
}

function retryAfterMs(response: Response): number {
  const retryAfter = response.headers.get("Retry-After");
  if (retryAfter == null) return DEFAULT_RETRY_AFTER_MS;
  const seconds = Number(retryAfter);
  if (!Number.isFinite(seconds)) return DEFAULT_RETRY_AFTER_MS;
  return Math.min(
    MAX_RETRY_AFTER_MS,
    Math.max(MIN_RETRY_AFTER_MS, Math.round(seconds * 1_000)),
  );
}

export async function fetchUsage(
  signal?: AbortSignal,
): Promise<UsagePollResult> {
  let response: Response;
  try {
    response = await fetch("/api/usage?include_daily=true", {
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new Error("Could not reach the server. Try again.");
  }

  if (response.status === 202) {
    return { status: "pending", retryAfterMs: retryAfterMs(response) };
  }
  if (response.status === 401) {
    throw new Error(
      "Your session has expired. Reload this page to sign in again.",
    );
  }
  if (response.status === 403) {
    throw new Error("You do not have permission to view usage data.");
  }
  if (response.status === 503) {
    throw new Error("Usage data is temporarily unavailable. Try again.");
  }
  if (!response.ok) {
    throw new Error("Could not load usage data. Try again.");
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error(INVALID_RESPONSE_MESSAGE);
  }
  if (!isUsageReport(payload)) {
    throw new Error(INVALID_RESPONSE_MESSAGE);
  }
  return { status: "ready", report: payload };
}

const WINDOW_KINDS = new Set(["session", "weekly", "monthly"]);
const UNAVAILABLE_REASONS = new Set([
  "unsupported",
  "not_signed_in",
  "probe_failed",
]);

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || typeof value === "string";
}

function isProviderLimitWindow(value: unknown): value is ProviderLimitWindow {
  return (
    isPlainObject(value) &&
    typeof value.id === "string" &&
    typeof value.kind === "string" &&
    WINDOW_KINDS.has(value.kind) &&
    typeof value.label === "string" &&
    isFiniteNumber(value.used_percent) &&
    isFiniteNumber(value.remaining_percent) &&
    (value.window_duration_mins === undefined ||
      isCount(value.window_duration_mins)) &&
    isOptionalString(value.resets_at) &&
    isOptionalString(value.detail)
  );
}

function isProviderLimitAccount(value: unknown): value is ProviderLimitAccount {
  return (
    isPlainObject(value) &&
    typeof value.id === "string" &&
    typeof value.provider === "string" &&
    typeof value.display_name === "string" &&
    isOptionalString(value.account_label) &&
    isOptionalString(value.plan) &&
    Array.isArray(value.model_keys) &&
    value.model_keys.every((key) => typeof key === "string") &&
    Array.isArray(value.windows) &&
    value.windows.every(isProviderLimitWindow) &&
    (value.unavailable_reason === undefined ||
      (typeof value.unavailable_reason === "string" &&
        UNAVAILABLE_REASONS.has(value.unavailable_reason))) &&
    isOptionalString(value.checked_at) &&
    typeof value.stale === "boolean" &&
    isCount(value.tokens_7d) &&
    (value.cost_usd_7d === null || isFiniteNumber(value.cost_usd_7d)) &&
    typeof value.cost_estimated === "boolean"
  );
}

function isProviderUsageDailyRow(
  value: unknown,
): value is ProviderUsageDailyRow {
  return (
    isPlainObject(value) &&
    typeof value.day === "string" &&
    isPlainObject(value.by_account) &&
    Object.values(value.by_account).every(isCount) &&
    isCount(value.unattributed_tokens)
  );
}

export function isProviderUsageSnapshot(
  value: unknown,
): value is ProviderUsageSnapshot {
  return (
    isPlainObject(value) &&
    value.version === 1 &&
    isOptionalString(value.request_id) &&
    typeof value.checked_at === "string" &&
    typeof value.partial === "boolean" &&
    Array.isArray(value.accounts) &&
    value.accounts.every(isProviderLimitAccount) &&
    Array.isArray(value.daily_7d) &&
    value.daily_7d.every(isProviderUsageDailyRow) &&
    isCount(value.unattributed_tokens_7d) &&
    (value.unattributed_cost_usd_7d === null ||
      isFiniteNumber(value.unattributed_cost_usd_7d))
  );
}

export async function fetchProviderLimits(
  refresh = false,
  signal?: AbortSignal,
): Promise<ProviderUsageSnapshot> {
  const url = refresh
    ? "/api/usage/provider-limits?refresh=true"
    : "/api/usage/provider-limits";
  let response: Response;
  try {
    response = await fetch(url, {
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new Error("Could not reach the server. Try again.");
  }
  if (response.status === 401) {
    throw new Error(
      "Your session has expired. Reload this page to sign in again.",
    );
  }
  if (response.status === 403) {
    throw new Error("You do not have permission to view provider limits.");
  }
  if (response.status === 503) {
    throw new Error("Provider limits are temporarily unavailable. Try again.");
  }
  if (!response.ok) {
    throw new Error("Could not load provider limits. Try again.");
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error(INVALID_RESPONSE_MESSAGE);
  }
  if (!isProviderUsageSnapshot(payload)) {
    throw new Error(INVALID_RESPONSE_MESSAGE);
  }
  return payload;
}
