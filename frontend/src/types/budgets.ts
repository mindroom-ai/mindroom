export interface BudgetUserStatus {
  user_id: string;
  spend_usd: number;
  limit_usd: number | null;
  over_budget: boolean;
}

export interface UnpricedModelUsage {
  provider: string;
  model: string;
  total_tokens: number;
}

export interface EnabledBudgetStatus {
  enabled: true;
  period_start: string;
  period_end: string;
  generated_at: string | null;
  default_limit_usd: number | null;
  fallback_model: string;
  users: BudgetUserStatus[];
  unpriced_models: UnpricedModelUsage[];
  coverage: { scanned_sources: number; unavailable_sources: number };
}

export type BudgetStatus = { enabled: false } | EnabledBudgetStatus;
