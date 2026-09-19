import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Dashboard } from "./Dashboard";
import { useConfigStore } from "@/store/configStore";
import type { Config } from "@/types/config";

const totals = {
  input_tokens: 0,
  output_tokens: 0,
  total_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  reasoning_tokens: 0,
  audio_input_tokens: 0,
  audio_output_tokens: 0,
  audio_total_tokens: 0,
};
const coverage = {
  scanned_sources: 1,
  unavailable_sources: 0,
  note: "Recorded runs",
};
const report = {
  schema_version: 1,
  scope: "admin",
  generated_at: "2026-09-19T12:00:00Z",
  totals,
  session_count: 888,
  breakdown: [],
  coverage,
  cumulative_model_breakdown: [],
  cumulative_model_coverage: coverage,
  user_breakdown: [],
  user_coverage: coverage,
  daily_breakdown: [
    { date: "2026-09-19", run_count: 42, totals, model_breakdown: [] },
  ],
  daily_coverage: coverage,
};
const schedule = {
  task_id: "brief",
  room_id: "lobby",
  room_alias: null,
  status: "pending",
  schedule_type: "once",
  next_run_at: "2026-09-20T01:30:00Z",
  execute_at: null,
  description: "Morning briefing",
  message: "Hello",
};
const config: Config = {
  agents: {},
  models: {},
  defaults: { markdown: true },
  router: { model: "default" },
  memory: { embedder: { provider: "openai", config: { model: "embedding" } } },
};
function Path() {
  return (
    <output aria-label="Current pathname">{useLocation().pathname}</output>
  );
}
function renderHome() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/dashboard"]}>
          <Dashboard />
          <Path />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}
function respond(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), { status });
}
function stubApis(
  usage: unknown = report,
  usageStatus = 200,
  schedules: unknown = { timezone: "America/Los_Angeles", tasks: [schedule] },
  scheduleStatus = 200,
) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (String(url).includes("/usage")) return respond(usage, usageStatus);
      if (String(url).includes("/schedules"))
        return respond(schedules, scheduleStatus);
      return respond({
        runtime_phase: "ready",
        admission_paused: false,
        active_matrix_operations: 0,
        active_openai_requests: 0,
        status: "idle",
      });
    }),
  );
}
beforeEach(() => {
  useConfigStore.setState({
    config,
    agents: [
      {
        id: "code",
        display_name: "Code",
        role: "Writes code",
        tools: [],
        skills: [],
        instructions: [],
        rooms: ["lobby"],
      },
    ],
    rooms: [
      {
        id: "lobby",
        display_name: "Lobby",
        description: "A place to begin",
        agents: ["code"],
      },
    ],
    teams: [],
    selectedAgentId: null,
    selectedRoomId: null,
    selectedTeamId: null,
  });
  stubApis();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Home", () => {
  it("shows daily runs, exact accessible bars, report date and service timezone", async () => {
    renderHome();
    expect(screen.getByRole("heading", { name: "Home" })).toBeVisible();
    expect(await screen.findByText("42")).toBeVisible();
    expect(screen.queryByText("888")).not.toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "2026-09-19: 42 runs" }),
    ).toBeVisible();
    expect(screen.getByText("Last 7 days · UTC")).toBeVisible();
    expect(screen.getByText("Report date: Sep 19, 2026 · UTC")).toBeVisible();
    expect(await screen.findByText("Morning briefing")).toBeVisible();
    expect(screen.getByText("America/Los_Angeles")).toBeVisible();
    expect(screen.getByText("6:30 PM")).toBeVisible();
    expect(screen.getByText("Sep 19")).toBeVisible();
  });
  it("keeps runtime and schedules useful when config and usage are unavailable", async () => {
    useConfigStore.setState({ config: null, agents: [], rooms: [] });
    stubApis({}, 503);
    renderHome();
    expect(await screen.findByText("Usage unavailable")).toBeVisible();
    expect(await screen.findByText("Idle")).toBeVisible();
    expect(await screen.findByText("Morning briefing")).toBeVisible();
    expect(screen.getByRole("button", { name: "Retry usage" })).toBeVisible();
  });
  it("shows partial usage honestly and does not fill missing days with zero", async () => {
    stubApis({
      ...report,
      daily_breakdown: [],
      daily_coverage: { ...coverage, unavailable_sources: 1 },
    });
    renderHome();
    expect(await screen.findByText("Partial data")).toBeVisible();
    expect(
      screen.getByRole("img", { name: "2026-09-19: unavailable" }),
    ).toBeVisible();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
  it("shows a real zero with complete empty coverage and useful empty schedules", async () => {
    stubApis({ ...report, daily_breakdown: [] }, 200, {
      timezone: "UTC",
      tasks: [],
    });
    renderHome();
    expect(await screen.findByText("0")).toBeVisible();
    expect(await screen.findByText("Nothing scheduled")).toBeVisible();
    expect(screen.getByRole("link", { name: /All schedules/ })).toHaveAttribute(
      "href",
      "/schedules",
    );
  });
  it.each([
    { timezone: "UTC", tasks: [null] },
    { timezone: "not/a-zone", tasks: [] },
  ])(
    "contains malformed schedules or timezone without hiding usage (%j)",
    async (bad) => {
      stubApis(report, 200, bad);
      renderHome();
      expect(await screen.findByText("Schedules unavailable")).toBeVisible();
      expect(await screen.findByText("42")).toBeVisible();
    },
  );
  it("polls 202 usage using Retry-After and stops once ready", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const original = vi.mocked(fetch).getMockImplementation()!;
    let usageCalls = 0;
    vi.mocked(fetch).mockImplementation(async (...args) => {
      if (String(args[0]).includes("/usage")) {
        usageCalls++;
        if (usageCalls === 1)
          return new Response("{}", {
            status: 202,
            headers: { "Retry-After": "1" },
          });
      }
      return original(...args);
    });
    renderHome();
    expect(await screen.findByText("Preparing usage report…")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    expect(await screen.findByText("42")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(usageCalls).toBe(2);
  });
  it("contains schedule failure without hiding usage", async () => {
    stubApis(report, 200, {}, 503);
    renderHome();
    expect(await screen.findByText("Schedules unavailable")).toBeVisible();
    expect(await screen.findByText("42")).toBeVisible();
  });
  it("opens real room and agent editors with their selection", async () => {
    const view = renderHome();
    fireEvent.click(screen.getByRole("button", { name: "Configure Lobby" }));
    expect(useConfigStore.getState().selectedRoomId).toBe("lobby");
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      "/rooms",
    );
    view.unmount();
    renderHome();
    fireEvent.click(screen.getByRole("button", { name: "Configure Code" }));
    expect(useConfigStore.getState().selectedAgentId).toBe("code");
    expect(useConfigStore.getState().selectedRoomId).toBeNull();
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      "/agents",
    );
  });
  it("retains the directory behind Browse workspace and comes back without changing route", async () => {
    renderHome();
    fireEvent.click(screen.getByRole("button", { name: "Browse workspace" }));
    expect(screen.getByRole("searchbox")).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "Writes code" },
    });
    expect(
      within(screen.getByLabelText("Workspace directory")).getAllByRole(
        "button",
      ),
    ).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Export config" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Back to home" }));
    expect(screen.getByRole("heading", { name: "Home" })).toBeVisible();
    expect(screen.getByLabelText("Current pathname")).toHaveTextContent(
      "/dashboard",
    );
  });
});
