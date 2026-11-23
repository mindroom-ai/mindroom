import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import contractFixture from "../../../../tests/fixtures/provider_usage_contract.json";
import type { ProviderUsageSnapshot } from "@/types/usage";
import { ProviderLimits } from "./ProviderLimits";

vi.stubGlobal("fetch", vi.fn());

function respond(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderProviderLimits() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProviderLimits />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(fetch).mockReset();
  vi.setSystemTime(new Date("2026-11-23T20:00:00Z"));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ProviderLimits", () => {
  it("renders contract accounts, remaining bars, pace ticks, countdowns, and 7d summaries", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(contractFixture));
    renderProviderLimits();

    const section = await screen.findByRole("region", {
      name: "Provider limits",
    });
    expect(await within(section).findByText("Claude Code")).toBeInTheDocument();
    expect(within(section).getByText("Max 20x")).toBeInTheDocument();
    expect(within(section).getByText("Codex")).toBeInTheDocument();
    expect(within(section).getByText("ChatGPT Pro")).toBeInTheDocument();
    expect(within(section).getByText("OpenRouter")).toBeInTheDocument();
    expect(within(section).getByText("$31.60 / $50.00 left")).toBeInTheDocument();
    expect(within(section).getByText("~$18.40 · 7d")).toBeInTheDocument();
    expect(within(section).getByText("Anthropic")).toBeInTheDocument();
    expect(within(section).getByText("Pay-as-you-go")).toBeInTheDocument();

    // Remaining capacity bars & countdowns.
    expect(within(section).getByText("62% left")).toBeInTheDocument();
    expect(within(section).getByText("Resets in 2h 24m")).toBeInTheDocument();
    expect(within(section).getByText("12% left")).toBeInTheDocument();
    expect(within(section).getByText("Resets in 3h 6m")).toBeInTheDocument();

    // Pace tick marks render on timed windows (4 timed windows across Claude + Codex, none on OpenRouter credit_limit).
    const ticks = within(section).getAllByTestId("pace-tick");
    expect(ticks).toHaveLength(4);
    // Claude 5-hour window: 144m remaining out of 300m = 48%.
    expect(ticks[0]).toHaveStyle({ left: "48%" });
  });

  it("shows stale notices, unavailable reasons, unlimited credit, and unattributed 7d tokens", async () => {
    const snapshot: ProviderUsageSnapshot = {
      version: 1,
      checked_at: "2026-11-23T20:00:00Z",
      partial: false,
      accounts: [
        {
          id: "codex:default",
          provider: "codex",
          display_name: "Codex",
          account_label: "ChatGPT Pro",
          plan: "ChatGPT Pro",
          model_keys: ["astra"],
          windows: [
            {
              id: "five_hour",
              kind: "session",
              label: "5-hour window",
              used_percent: 40,
              remaining_percent: 60,
              window_duration_mins: 300,
              resets_at: "2026-11-23T22:00:00Z",
            },
          ],
          checked_at: "2026-11-23T19:30:00Z",
          stale: true,
          tokens_7d: 50000,
          cost_usd_7d: null,
          cost_estimated: false,
        },
        {
          id: "claude_code:default",
          provider: "claude_code",
          display_name: "Claude Code",
          model_keys: ["opus"],
          windows: [],
          unavailable_reason: "not_signed_in",
          checked_at: "2026-11-23T20:00:00Z",
          stale: false,
          tokens_7d: 0,
          cost_usd_7d: null,
          cost_estimated: false,
        },
        {
          id: "openrouter:1",
          provider: "openrouter",
          display_name: "OpenRouter",
          account_label: "Unlimited credit",
          plan: "Unlimited credit",
          model_keys: ["neon"],
          windows: [],
          checked_at: "2026-11-23T20:00:00Z",
          stale: false,
          tokens_7d: 10000,
          cost_usd_7d: 1.5,
          cost_estimated: true,
        },
      ],
      daily_7d: [],
      unattributed_tokens_7d: 250000,
      unattributed_cost_usd_7d: 2.1,
    };
    vi.mocked(fetch).mockResolvedValue(respond(snapshot));
    renderProviderLimits();

    expect(await screen.findByText("Stale")).toBeInTheDocument();
    expect(
      screen.getByText(/Showing cached reading after a failed refresh/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Not signed in")).toBeInTheDocument();
    expect(screen.getByText("Unlimited credit")).toBeInTheDocument();
    expect(
      screen.getByText(/250,000 additional tokens.*~\$2\.10/i),
    ).toBeInTheDocument();
  });

  it("forces refresh via ?refresh=true and recovers from load errors", async () => {
    const refreshed: ProviderUsageSnapshot = {
      ...(contractFixture as ProviderUsageSnapshot),
      accounts: [
        {
          ...(contractFixture.accounts[0] as ProviderUsageSnapshot["accounts"][number]),
          windows: [
            {
              id: "five_hour",
              kind: "session",
              label: "5-hour window",
              used_percent: 10,
              remaining_percent: 90,
              window_duration_mins: 300,
              resets_at: "2026-11-23T24:00:00Z",
            },
          ],
        },
      ],
    };
    vi.mocked(fetch)
      .mockResolvedValueOnce(respond({}, 503))
      .mockResolvedValueOnce(respond(contractFixture))
      .mockResolvedValueOnce(respond(refreshed));

    renderProviderLimits();
    expect(await screen.findByRole("alert")).toHaveTextContent(/unavailable/i);

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("62% left")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Refresh limits" }),
    );
    await waitFor(() =>
      expect(screen.getByText("90% left")).toBeInTheDocument(),
    );
    expect(vi.mocked(fetch)).toHaveBeenLastCalledWith(
      "/api/usage/provider-limits?refresh=true",
      expect.objectContaining({ cache: "no-store", credentials: "same-origin" }),
    );
  });
});
