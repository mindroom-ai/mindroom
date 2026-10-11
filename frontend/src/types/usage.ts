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
