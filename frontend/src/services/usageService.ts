import { API_BASE_URL } from "@/lib/api";
import type { TokenTotals, UsagePollResult, UsageReport } from "@/types/usage";

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isTokenTotals(value: unknown): value is TokenTotals {
  return (
    isRecord(value) && TOKEN_FIELDS.every((field) => isCount(value[field]))
  );
}

function isCoverage(value: unknown): boolean {
  return (
    isRecord(value) &&
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
    isRecord(value) && isTokenTotals(value.totals) && isCount(value[count])
  );
}

function isRequesterRow(value: unknown): boolean {
  return (
    isUsageRow(value, "run_count") &&
    (value.user_id === null || typeof value.user_id === "string")
  );
}

function isModelRow(value: unknown): boolean {
  return (
    isUsageRow(value, "session_count") &&
    typeof value.provider === "string" &&
    typeof value.model === "string"
  );
}

function isEntityRow(value: unknown): boolean {
  return (
    isUsageRow(value, "session_count") &&
    value.dimension === "entity" &&
    typeof value.key === "string" &&
    isCount(value.run_count) &&
    isTokenTotals(value.retained_run_totals) &&
    Array.isArray(value.user_breakdown) &&
    value.user_breakdown.every(isRequesterRow)
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
  return isUsageRow(value, "run_count") && isDay(value.date);
}

function isUsageReport(value: unknown): value is UsageReport {
  if (!isRecord(value)) return false;

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
    value.cumulative_model_breakdown.every(isModelRow) &&
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

function isAbortError(error: unknown): boolean {
  return isRecord(error) && error.name === "AbortError";
}

export async function fetchUsage(
  signal?: AbortSignal,
): Promise<UsagePollResult> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/usage?include_daily=true`, {
      cache: "no-store",
      credentials: "same-origin",
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
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
