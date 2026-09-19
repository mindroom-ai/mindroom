import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Usage } from "./Usage";

const totals = (total: number) => ({
  input_tokens: total * 0.8,
  output_tokens: total * 0.2,
  total_tokens: total,
  cache_read_tokens: total * 0.5,
  cache_write_tokens: total * 0.1,
  reasoning_tokens: 0,
  audio_input_tokens: 0,
  audio_output_tokens: 0,
  audio_total_tokens: 0,
});
const coverage = {
  scanned_sources: 1,
  unavailable_sources: 0,
  note: "Recorded usage",
};
const model = {
  provider: "example",
  model: "sample-model",
  totals: totals(1000),
  session_count: 3,
};
const requester = {
  user_id: "@alice:example.test",
  totals: totals(100),
  run_count: 1,
  model_breakdown: [],
  daily_breakdown: [],
};
const report = {
  schema_version: 1,
  scope: "admin",
  generated_at: "2026-09-19T12:00:00Z",
  totals: totals(1000),
  session_count: 3,
  coverage,
  breakdown: [
    {
      dimension: "entity",
      key: "research",
      totals: totals(1000),
      session_count: 3,
      cumulative_model_breakdown: [model],
      retained_run_totals: totals(200),
      run_count: 2,
      user_breakdown: [requester, { ...requester, user_id: null }],
    },
  ],
  model_breakdown: [],
  model_coverage: coverage,
  cumulative_model_breakdown: [model],
  cumulative_model_coverage: coverage,
  user_breakdown: [requester, { ...requester, user_id: null }],
  user_coverage: coverage,
  daily_breakdown: [
    {
      date: "2026-08-01",
      totals: totals(100),
      run_count: 1,
      model_breakdown: [],
    },
    {
      date: "2026-09-19",
      totals: totals(100),
      run_count: 1,
      model_breakdown: [],
    },
  ],
  daily_coverage: coverage,
  private_agent_breakdown: [],
  private_agent_coverage: coverage,
};

function respond(payload: unknown = report, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}
function renderUsage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    ...render(
      <QueryClientProvider client={client}>
        <Usage />
      </QueryClientProvider>,
    ),
    client,
  };
}

beforeEach(() => vi.mocked(fetch).mockReset());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Usage", () => {
  it("keeps cumulative totals separate from retained requester detail", async () => {
    vi.mocked(fetch).mockResolvedValue(respond());
    renderUsage();
    const agent = await screen.findByRole("button", { name: "research" });
    expect(
      within(
        screen.getByRole("region", { name: "All-time recorded usage" }),
      ).getByText("1,000"),
    ).toBeInTheDocument();
    agent.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("@alice:example.test")).toBeInTheDocument();
    expect(within(dialog).getByText("Unknown requester")).toBeInTheDocument();
    expect(within(dialog).getAllByText("100")).toHaveLength(2);
    expect(
      within(dialog).getByText(/200 tokens across 2 recorded runs/),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText("1,000")).not.toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(agent).toHaveFocus());
  });

  it("filters only daily activity by UTC date and keeps model totals cumulative", async () => {
    vi.mocked(fetch).mockResolvedValue(respond());
    renderUsage();
    await screen.findByRole("button", { name: "research" });
    const activity = screen.getByRole("region", { name: "Daily activity" });
    await userEvent.click(within(activity).getByText("View daily data"));
    const daily = within(activity).getByRole("table", { name: "Daily data" });
    expect(within(daily).queryByText("2026-08-01")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Activity period"), {
      target: { value: "all" },
    });
    expect(within(daily).getByText("2026-08-01")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Models" }));
    const table = screen.getByRole("table", { name: "Models" });
    expect(within(table).getByText("sample-model")).toBeInTheDocument();
    expect(within(table).getByText("1,000")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Token metric"), {
      target: { value: "cache_write_tokens" },
    });
    expect(within(table).getByText("100")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Search breakdown"), {
      target: { value: "missing" },
    });
    expect(within(table).queryByText("sample-model")).not.toBeInTheDocument();
    expect(screen.getByText("No matching usage.")).toBeInTheDocument();
  });

  it("polls preparation then renders the ready report", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        respond({ status: "pending" }, 202, { "Retry-After": "1" }),
      )
      .mockResolvedValue(respond());
    renderUsage();
    expect(await screen.findByRole("status")).toHaveTextContent(
      /Preparing usage/,
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(
      await screen.findByRole("button", { name: "research" }),
    ).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("stops polling and discards reports when leaving the page", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(fetch).mockResolvedValue(
      respond({ status: "pending" }, 202, { "Retry-After": "1" }),
    );
    const { unmount, client } = renderUsage();
    await screen.findByRole("status");
    unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(client.getQueryData(["usage"])).toBeUndefined();
  });

  it("hides a previous report when refreshing loses access and allows retry", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(respond())
      .mockResolvedValueOnce(respond({}, 403))
      .mockResolvedValue(respond());
    renderUsage();
    await screen.findByRole("button", { name: "research" });
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /access|permission/i,
    );
    expect(
      screen.queryByRole("button", { name: "research" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("button", { name: "research" }),
    ).toBeInTheDocument();
  });

  it("distinguishes an empty report from unreadable usage sources", async () => {
    vi.mocked(fetch).mockResolvedValue(
      respond({
        ...report,
        totals: totals(0),
        session_count: 0,
        breakdown: [],
        cumulative_model_breakdown: [],
        user_breakdown: [],
        daily_breakdown: [],
        coverage: { ...coverage, unavailable_sources: 1 },
      }),
    );
    renderUsage();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not be read/,
    );
    expect(
      screen.getByText("No recorded daily activity in this period."),
    ).toBeInTheDocument();
    expect(screen.getByText("No matching usage.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
  });
});
