export interface TokenTotals {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  audio_input_tokens: number;
  audio_output_tokens: number;
  audio_total_tokens: number;
}

export interface UsageCoverage {
  scanned_sources: number;
  unavailable_sources: number;
  note: string;
}

export interface UsageModelRow {
  provider: string;
  model: string;
  totals: TokenTotals;
  run_count: number;
}

export interface UsageCumulativeModelRow {
  provider: string;
  model: string;
  totals: TokenTotals;
  session_count: number;
}

export interface UsageDailyRow {
  date: string;
  totals: TokenTotals;
  run_count: number;
  model_breakdown: UsageModelRow[];
}

export interface UsageUserRow {
  user_id: string | null;
  totals: TokenTotals;
  run_count: number;
  model_breakdown: UsageModelRow[];
  daily_breakdown: UsageDailyRow[];
}

export interface UsageEntityRow {
  dimension: "entity";
  key: string;
  totals: TokenTotals;
  session_count: number;
  cumulative_model_breakdown: UsageCumulativeModelRow[];
  retained_run_totals: TokenTotals;
  run_count: number;
  user_breakdown: UsageUserRow[];
}

export interface UsageReport {
  schema_version: 1;
  generated_at: string;
  scope: "admin";
  totals: TokenTotals;
  session_count: number;
  breakdown: UsageEntityRow[];
  coverage: UsageCoverage;
  cumulative_model_breakdown: UsageCumulativeModelRow[];
  cumulative_model_coverage: UsageCoverage;
  user_breakdown: UsageUserRow[];
  user_coverage: UsageCoverage;
  daily_breakdown: UsageDailyRow[];
  daily_coverage: UsageCoverage;
}

export type UsagePollResult =
  | { status: "pending"; retryAfterMs: number }
  | { status: "ready"; report: UsageReport };

export type ProviderLimitWindowKind = "session" | "weekly" | "monthly";

export type ProviderUnavailableReason =
  | "unsupported"
  | "not_signed_in"
  | "probe_failed";

export interface ProviderLimitWindow {
  id: string;
  kind: ProviderLimitWindowKind;
  label: string;
  used_percent: number;
  remaining_percent: number;
  window_duration_mins?: number;
  resets_at?: string;
  detail?: string;
}

export interface ProviderLimitAccount {
  id: string;
  provider: string;
  display_name: string;
  account_label?: string;
  plan?: string;
  model_keys: string[];
  windows: ProviderLimitWindow[];
  unavailable_reason?: ProviderUnavailableReason;
  checked_at?: string;
  stale: boolean;
  tokens_7d: number;
  cost_usd_7d: number | null;
  cost_estimated: boolean;
}

export interface ProviderUsageDailyRow {
  day: string;
  by_account: Record<string, number>;
  unattributed_tokens: number;
}

export interface ProviderUsageSnapshot {
  version: 1;
  request_id?: string;
  checked_at: string;
  partial: boolean;
  accounts: ProviderLimitAccount[];
  daily_7d: ProviderUsageDailyRow[];
  unattributed_tokens_7d: number;
  unattributed_cost_usd_7d: number | null;
}
