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

// One run can use both providers. Alice also has 50 undated tokens.
const modelUsage = (provider: string, total: number, count: number) => ({
  provider,
  model: "sample-model",
  totals: totals(total),
  run_count: count,
});
const day = (
  date: string,
  total: number,
  count: number,
  models: ReturnType<typeof modelUsage>[],
) => ({
  date,
  totals: totals(total),
  run_count: count,
  model_breakdown: models,
});
const aliceResearch = {
  ...requester,
  totals: totals(150),
  run_count: 2,
  model_breakdown: [
    modelUsage("example", 110, 2),
    modelUsage("alternate", 40, 1),
  ],
  daily_breakdown: [
    day("2026-09-19", 100, 1, [
      modelUsage("example", 60, 1),
      modelUsage("alternate", 40, 1),
    ]),
  ],
};
const unknownResearch = {
  ...requester,
  user_id: null,
  model_breakdown: [modelUsage("example", 100, 1)],
  daily_breakdown: [day("2026-09-19", 100, 1, [modelUsage("example", 100, 1)])],
};
const aliceWriting = {
  ...requester,
  totals: totals(200),
  model_breakdown: [modelUsage("example", 200, 1)],
  daily_breakdown: [day("2026-09-18", 200, 1, [modelUsage("example", 200, 1)])],
};
const samWriting = {
  ...requester,
  user_id: "@sam:example.test",
  totals: totals(300),
  model_breakdown: [modelUsage("alternate", 300, 1)],
  daily_breakdown: [
    day("2026-09-19", 300, 1, [modelUsage("alternate", 300, 1)]),
  ],
};
const cumulativeModel = (provider: string, total: number, count: number) => ({
  provider,
  model: "sample-model",
  totals: totals(total),
  session_count: count,
});
const drilldownReport = {
  ...report,
  totals: totals(1700),
  session_count: 6,
  breakdown: [
    {
      ...report.breakdown[0],
      retained_run_totals: totals(250),
      run_count: 3,
      cumulative_model_breakdown: [
        cumulativeModel("example", 800, 3),
        cumulativeModel("alternate", 200, 1),
      ],
      user_breakdown: [aliceResearch, unknownResearch],
    },
    {
      ...report.breakdown[0],
      key: "writing",
      totals: totals(700),
      retained_run_totals: totals(500),
      run_count: 2,
      cumulative_model_breakdown: [
        cumulativeModel("example", 300, 2),
        cumulativeModel("alternate", 400, 2),
      ],
      user_breakdown: [aliceWriting, samWriting],
    },
  ],
  cumulative_model_breakdown: [
    cumulativeModel("example", 1100, 5),
    cumulativeModel("alternate", 600, 3),
  ],
  user_breakdown: [
    {
      ...requester,
      totals: totals(350),
      run_count: 3,
      model_breakdown: [
        modelUsage("example", 310, 3),
        modelUsage("alternate", 40, 1),
      ],
      daily_breakdown: [
        ...aliceWriting.daily_breakdown,
        ...aliceResearch.daily_breakdown,
      ],
    },
    unknownResearch,
    samWriting,
  ],
  daily_breakdown: [
    ...aliceWriting.daily_breakdown,
    day("2026-09-19", 500, 3, [
      modelUsage("example", 160, 2),
      modelUsage("alternate", 340, 2),
    ]),
  ],
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
    expect(
      within(dialog).getByRole("region", { name: "All-time recorded usage" }),
    ).toHaveTextContent("1,000");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(agent).toHaveFocus());
  });

  it("shows an agent's cumulative models and combines requester days without inventing undated activity", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(drilldownReport));
    renderUsage();
    await userEvent.click(
      await screen.findByRole("button", { name: "research" }),
    );
    const dialog = within(screen.getByRole("dialog"));
    expect(dialog.getByRole("table", { name: "Requesters" })).toHaveTextContent(
      "150",
    );
    expect(
      dialog.getByText(/250 tokens across 3 recorded runs/),
    ).toBeInTheDocument();

    await userEvent.click(dialog.getByRole("tab", { name: "Models" }));
    const models = within(dialog.getByRole("table", { name: "Models" }));
    expect(models.getByText("800")).toBeInTheDocument();
    expect(models.getByText("200")).toBeInTheDocument();
    expect(models.getByText("Stored sessions")).toBeInTheDocument();

    await userEvent.click(dialog.getByRole("tab", { name: "Daily activity" }));
    await userEvent.click(dialog.getByText("View daily data"));
    const daily = within(dialog.getByRole("table", { name: "Daily data" }));
    expect(
      daily.getByRole("row", { name: "2026-09-19 2 200" }),
    ).toBeInTheDocument();
    expect(daily.queryByText("250")).not.toBeInTheDocument();
    fireEvent.change(dialog.getByLabelText("Token metric"), {
      target: { value: "cache_write_tokens" },
    });
    expect(
      daily.getByRole("row", { name: "2026-09-19 2 20" }),
    ).toBeInTheDocument();
  });

  it("drills into a requester across agents, models, and dated runs", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(drilldownReport));
    renderUsage();
    await screen.findByRole("button", { name: "research" });
    await userEvent.click(screen.getByRole("tab", { name: "Requesters" }));
    const person = screen.getByRole("button", { name: "@alice:example.test" });
    person.focus();
    await userEvent.keyboard("{Enter}");
    const dialog = within(screen.getByRole("dialog"));
    expect(
      dialog.getByRole("region", { name: "Recorded runs only" }),
    ).toHaveTextContent("350");
    const agents = within(
      dialog.getByRole("table", { name: "Agents & teams" }),
    );
    expect(
      agents.getByRole("row", { name: "research 2 150" }),
    ).toBeInTheDocument();
    expect(
      agents.getByRole("row", { name: "writing 1 200" }),
    ).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("tab", { name: "Models" }));
    const models = within(dialog.getByRole("table", { name: "Models" }));
    expect(
      models.getByRole("row", { name: "sample-model example 3 310" }),
    ).toBeInTheDocument();
    expect(
      models.getByRole("row", { name: "sample-model alternate 1 40" }),
    ).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("tab", { name: "Daily activity" }));
    await userEvent.click(dialog.getByText("View daily data"));
    const daily = within(dialog.getByRole("table", { name: "Daily data" }));
    expect(
      daily.getByRole("row", { name: "2026-09-18 1 200" }),
    ).toBeInTheDocument();
    expect(
      daily.getByRole("row", { name: "2026-09-19 1 100" }),
    ).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(person).toHaveFocus());

    await userEvent.click(
      screen.getByRole("button", { name: "Unknown requester" }),
    );
    expect(
      within(screen.getByRole("dialog")).getByRole("table", {
        name: "Agents & teams",
      }),
    ).toHaveTextContent("research");
    expect(
      within(screen.getByRole("dialog")).queryByText("writing"),
    ).not.toBeInTheDocument();
  });

  it("keeps model providers distinct and uses model-specific daily run counts", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(drilldownReport));
    renderUsage();
    await screen.findByRole("button", { name: "research" });
    await userEvent.click(screen.getByRole("tab", { name: "Models" }));
    const modelButton = screen.getByRole("button", {
      name: "sample-model (example)",
    });
    await userEvent.click(modelButton);
    const dialog = within(screen.getByRole("dialog"));
    expect(
      dialog.getByRole("region", { name: "All-time recorded usage" }),
    ).toHaveTextContent("1,100");
    const agents = within(
      dialog.getByRole("table", { name: "Agents & teams" }),
    );
    expect(
      agents.getByRole("row", { name: "research 3 800" }),
    ).toBeInTheDocument();
    expect(
      agents.getByRole("row", { name: "writing 2 300" }),
    ).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("tab", { name: "Requesters" }));
    const users = within(dialog.getByRole("table", { name: "Requesters" }));
    expect(
      users.getByRole("row", { name: "@alice:example.test 3 310" }),
    ).toBeInTheDocument();
    expect(
      users.getByRole("row", { name: "Unknown requester 1 100" }),
    ).toBeInTheDocument();
    expect(users.queryByText("@sam:example.test")).not.toBeInTheDocument();
    await userEvent.click(dialog.getByRole("tab", { name: "Daily activity" }));
    await userEvent.click(dialog.getByText("View daily data"));
    expect(
      dialog.getByRole("row", { name: "2026-09-19 2 160" }),
    ).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(modelButton).toHaveFocus());
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

  it("returns focus to search when an in-flight refresh removes the open row", async () => {
    let finishRefresh!: (response: Response) => void;
    vi.mocked(fetch)
      .mockResolvedValueOnce(respond())
      .mockReturnValueOnce(
        new Promise((resolve) => {
          finishRefresh = resolve;
        }),
      )
      .mockResolvedValue(respond());
    renderUsage();
    await screen.findByRole("button", { name: "research" });
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await userEvent.click(screen.getByRole("button", { name: "research" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await act(async () => finishRefresh(respond({ ...report, breakdown: [] })));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("textbox", { name: "Search breakdown" }),
      ).toHaveFocus(),
    );

    // Reappearing usage must not reopen a detail panel the user already left.
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await screen.findByRole("button", { name: "research" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
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
