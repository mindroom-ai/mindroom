import { API_BASE_URL } from "@/lib/api";
import type { BudgetStatus } from "@/types/budgets";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value != null && typeof value === "object" && !Array.isArray(value);
}

function isBudgetStatus(value: unknown): value is BudgetStatus {
  if (!isRecord(value) || typeof value.enabled !== "boolean") return false;
  if (!value.enabled) return true;
  return (
    typeof value.period_start === "string" &&
    typeof value.period_end === "string" &&
    typeof value.fallback_model === "string" &&
    Array.isArray(value.users) &&
    value.users.every(
      (user) =>
        isRecord(user) &&
        typeof user.user_id === "string" &&
        typeof user.spend_usd === "number" &&
        typeof user.over_budget === "boolean",
    ) &&
    Array.isArray(value.unpriced_models)
  );
}

export async function fetchBudgets(
  signal?: AbortSignal,
): Promise<BudgetStatus> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/budgets`, {
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
    throw new Error("You do not have permission to view budgets.");
  }
  if (response.status === 503) {
    throw new Error(
      "Spend is unavailable until the MindRoom runtime is running.",
    );
  }
  if (!response.ok) {
    throw new Error("Could not load budget status. Try again.");
  }
  const payload: unknown = await response.json();
  if (!isBudgetStatus(payload)) {
    throw new Error("Could not read budget status from the server. Try again.");
  }
  return payload;
}
