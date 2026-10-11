import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Budgets } from "./Budgets";
import { useConfigStore } from "@/store/configStore";
import type { Config } from "@/types/config";

const { mockToast } = vi.hoisted(() => ({ mockToast: vi.fn() }));

vi.mock("@/store/configStore");
vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

const mockUpdateConfigValue = vi.fn();
const mockSaveConfig = vi.fn();

const status = {
  enabled: true,
  period_start: "2026-10-01",
  period_end: "2026-11-01",
  generated_at: "2026-10-09T12:00:00+00:00",
  default_limit_usd: 20,
  fallback_model: "luna",
  users: [
    {
      user_id: "@alice:example.org",
      spend_usd: 25.5,
      limit_usd: 20,
      over_budget: true,
      override_usd: null,
      updated_at: null,
      updated_by: null,
    },
    {
      user_id: "@bob:example.org",
      spend_usd: 3.25,
      limit_usd: 100,
      over_budget: false,
      override_usd: 100,
      updated_at: "2026-10-09T12:00:00+00:00",
      updated_by: "budget-service",
    },
  ],
  unpriced_models: [
    { provider: "Ollama", model: "qwen3.8:27b", total_tokens: 12345 },
  ],
  coverage: { scanned_sources: 3, unavailable_sources: 0 },
};

function budgetConfig(overrides: Partial<Config> = {}): Config {
  return {
    models: {
      astra: {
        provider: "openai",
        id: "gpt-6-astra",
        pricing: { input: 5, output: 30 },
      },
      luna: {
        provider: "openai",
        id: "gpt-6-luna",
        pricing: { input: 0.2, output: 1.25 },
      },
      local: { provider: "ollama", id: "qwen3.8:27b" },
    },
    budgets: {
      monthly_limit_usd: 20,
      fallback_model: "luna",
    },
    ...overrides,
  } as Config;
}

function setStore(config: Config | null, isDirty = true) {
  vi.mocked(useConfigStore).mockReturnValue({
    config,
    isDirty,
    isLoading: false,
    saveConfig: mockSaveConfig,
    updateConfigValue: mockUpdateConfigValue,
  } as unknown as ReturnType<typeof useConfigStore>);
}

function respond(payload: unknown, statusCode = 200) {
  return new Response(JSON.stringify(payload), {
    status: statusCode,
    headers: { "Content-Type": "application/json" },
  });
}

/** Answer status reads with ``statusPayload`` and cap writes with ``write()``. */
function routeFetch(
  statusPayload: unknown = status,
  write: () => Response = () => respond({}),
) {
  vi.mocked(fetch).mockImplementation(async (_input, init) =>
    init?.method === "PUT" || init?.method === "DELETE"
      ? write()
      : respond(statusPayload),
  );
}

function writes() {
  return vi
    .mocked(fetch)
    .mock.calls.filter(
      ([, init]) => init?.method === "PUT" || init?.method === "DELETE",
    )
    .map(([input, init]) => [String(input), init?.method, init?.body]);
}

function renderBudgets() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const page = () => (
    <QueryClientProvider client={client}>
      <Budgets />
    </QueryClientProvider>
  );
  const result = render(page());
  // The mocked store is not reactive; rerender to show a draft edit.
  return { ...result, client, rerenderPage: () => result.rerender(page()) };
}

beforeEach(() => {
  vi.mocked(fetch).mockReset();
  mockUpdateConfigValue.mockReset();
  mockSaveConfig.mockReset();
  mockToast.mockReset();
  setStore(budgetConfig());
});
afterEach(() => cleanup());

describe("Budgets", () => {
  it("shows each user's month-to-date spend against their cap", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));

    renderBudgets();

    const alice = await screen.findByRole("row", {
      name: /@alice:example.org/,
    });
    expect(within(alice).getByText("$25.50")).toBeInTheDocument();
    expect(
      within(alice).getByText("Over budget, using luna"),
    ).toBeInTheDocument();
    const bob = screen.getByRole("row", { name: /@bob:example.org/ });
    expect(within(bob).getByText("$3.25")).toBeInTheDocument();
    expect(within(bob).getByText("Within budget")).toBeInTheDocument();
    expect(
      within(bob).getByRole("textbox", {
        name: "Monthly cap for @bob:example.org",
      }),
    ).toHaveValue("100");
    expect(within(bob).getByText("Set by budget-service")).toBeInTheDocument();
    expect(within(alice).queryByText(/Set by/)).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/api/budgets"),
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("keeps cents of a cent visible for small spend and caps", async () => {
    setStore(
      budgetConfig({
        budgets: { monthly_limit_usd: 0.005, fallback_model: "luna" },
      }),
    );
    vi.mocked(fetch).mockResolvedValue(
      respond({
        ...status,
        default_limit_usd: 0.005,
        users: [
          {
            user_id: "@alice:example.org",
            spend_usd: 0.011,
            limit_usd: 0.005,
            over_budget: true,
            override_usd: null,
            updated_at: null,
            updated_by: null,
          },
        ],
      }),
    );

    renderBudgets();

    const alice = await screen.findByRole("row", {
      name: /@alice:example.org/,
    });
    expect(within(alice).getByText("$0.011")).toBeInTheDocument();
    expect(
      within(alice).getByRole("textbox", {
        name: "Monthly cap for @alice:example.org",
      }),
    ).toHaveAttribute("placeholder", "Default ($0.005)");
  });

  it("marks cache prices that fall back to the input price", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    expect(
      await screen.findByRole("textbox", { name: "astra cache read price" }),
    ).toHaveAttribute("placeholder", "= input");
  });

  it("flags a model priced without both required prices", async () => {
    setStore(
      budgetConfig({
        models: {
          luna: {
            provider: "openai",
            id: "gpt-6-luna",
            pricing: { input: 0.2 },
          },
        } as Config["models"],
      }),
    );
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    const luna = await screen.findByRole("row", {
      name: /openai \/ gpt-6-luna/,
    });
    expect(
      within(luna).getByText("Set both input and output prices."),
    ).toBeInTheDocument();
  });

  it("saves a user's cap when the field is left and clears it back to the default", async () => {
    routeFetch();
    renderBudgets();
    const aliceCap = await screen.findByRole("textbox", {
      name: "Monthly cap for @alice:example.org",
    });

    fireEvent.change(aliceCap, { target: { value: "50" } });
    expect(writes()).toEqual([]);
    fireEvent.blur(aliceCap);

    const bobCap = screen.getByRole("textbox", {
      name: "Monthly cap for @bob:example.org",
    });
    fireEvent.change(bobCap, { target: { value: "" } });
    fireEvent.keyDown(bobCap, { key: "Enter" });

    await waitFor(() =>
      expect(writes()).toEqual([
        [
          "/api/budgets/users/%40alice%3Aexample.org",
          "PUT",
          JSON.stringify({ limit_usd: 50 }),
        ],
        ["/api/budgets/users/%40bob%3Aexample.org", "DELETE", undefined],
      ]),
    );
    expect(mockUpdateConfigValue).not.toHaveBeenCalled();
  });

  it("leaves an unchanged cap alone when the field is left", async () => {
    routeFetch();
    renderBudgets();

    fireEvent.blur(
      await screen.findByRole("textbox", {
        name: "Monthly cap for @bob:example.org",
      }),
    );

    expect(writes()).toEqual([]);
  });

  it("adds a cap for a user who has not spent anything yet", async () => {
    routeFetch();
    renderBudgets();
    await screen.findByRole("row", { name: /@alice:example.org/ });

    fireEvent.change(screen.getByPlaceholderText("@user:example.com"), {
      target: { value: "@carol:example.org" },
    });
    fireEvent.change(screen.getByRole("textbox", { name: "New user cap" }), {
      target: { value: "5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));

    await waitFor(() =>
      expect(writes()).toEqual([
        [
          "/api/budgets/users/%40carol%3Aexample.org",
          "PUT",
          JSON.stringify({ limit_usd: 5 }),
        ],
      ]),
    );
    await waitFor(() =>
      expect(
        vi
          .mocked(fetch)
          .mock.calls.filter(([, init]) => init?.method === undefined),
      ).toHaveLength(2),
    );
  });

  it("shows why a cap could not be saved", async () => {
    routeFetch(status, () =>
      respond({ detail: "Caps apply only to people, not agents or bots" }, 422),
    );
    renderBudgets();
    const aliceCap = await screen.findByRole("textbox", {
      name: "Monthly cap for @alice:example.org",
    });

    fireEvent.change(aliceCap, { target: { value: "50" } });
    fireEvent.blur(aliceCap);

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "Caps apply only to people, not agents or bots",
          variant: "destructive",
        }),
      ),
    );
  });

  it("edits the default cap and the fallback model", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(
      await screen.findByRole("textbox", { name: "Default monthly cap" }),
      {
        target: { value: "" },
      },
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["budgets", "monthly_limit_usd"],
      undefined,
    );

    fireEvent.change(screen.getByRole("combobox", { name: "Fallback model" }), {
      target: { value: "local" },
    });
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["budgets", "fallback_model"],
      "local",
    );
  });

  it("flags an invalid amount and shows the saved amount once the field is left", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    const cap = await screen.findByRole("textbox", {
      name: "Default monthly cap",
    });
    fireEvent.change(cap, { target: { value: "-1" } });

    expect(cap).toHaveAttribute("aria-invalid", "true");
    expect(mockUpdateConfigValue).not.toHaveBeenCalled();

    fireEvent.blur(cap);

    expect(cap).toHaveValue("20");
    expect(cap).toHaveAttribute("aria-invalid", "false");
  });

  it("shows spend as unknown until budgets report it", async () => {
    vi.mocked(fetch).mockResolvedValue(
      respond({ ...status, generated_at: null, users: [status.users[1]] }),
    );
    renderBudgets();

    const bob = (
      await screen.findByRole("textbox", {
        name: "Monthly cap for @bob:example.org",
      })
    ).closest("tr")!;
    expect(within(bob).getByText("—")).toBeInTheDocument();
    expect(within(bob).getByText("Waiting for spend")).toBeInTheDocument();
  });

  it("ignores edits to a cap while it is saving", async () => {
    let finishPut: (response: Response) => void = () => {};
    routeFetch(status, () => respond({}));
    vi.mocked(fetch).mockImplementation(async (_input, init) =>
      init?.method === "PUT"
        ? new Promise<Response>((resolve) => {
            finishPut = resolve;
          })
        : respond(status),
    );
    renderBudgets();
    const bobCap = await screen.findByRole("textbox", {
      name: "Monthly cap for @bob:example.org",
    });

    fireEvent.change(bobCap, { target: { value: "50" } });
    fireEvent.keyDown(bobCap, { key: "Enter" });
    await waitFor(() => expect(bobCap).toHaveAttribute("readonly"));
    fireEvent.blur(bobCap);

    expect(writes()).toEqual([
      [
        "/api/budgets/users/%40bob%3Aexample.org",
        "PUT",
        JSON.stringify({ limit_usd: 50 }),
      ],
    ]);
    finishPut(respond({}));
    await waitFor(() => expect(bobCap).not.toHaveAttribute("readonly"));
  });

  it("lists per-user caps once budgets are saved, before the runtime reports them", async () => {
    setStore(budgetConfig(), false);
    vi.mocked(fetch).mockResolvedValue(respond({ enabled: false }));
    const { client } = renderBudgets();

    await waitFor(() =>
      expect(client.getQueryData(["budgets"])).toEqual({ enabled: false }),
    );
    expect(
      screen.getByRole("button", { name: "Add user" }),
    ).toBeInTheDocument();
  });

  it("asks to save budgets before per-user caps can be set", async () => {
    vi.mocked(fetch).mockResolvedValue(respond({ enabled: false }));
    renderBudgets();

    expect(
      await screen.findByText(
        "Save to turn on budgets, then set per-user caps here.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Add user" }),
    ).not.toBeInTheDocument();
  });

  it("turns budgets on with the cheapest priced model as fallback", async () => {
    setStore(budgetConfig({ budgets: undefined }));
    vi.mocked(fetch).mockResolvedValue(respond({ enabled: false }));
    renderBudgets();

    fireEvent.click(
      await screen.findByRole("button", { name: "Turn on budgets" }),
    );

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(["budgets"], {
      fallback_model: "luna",
    });
    expect(
      screen.queryByRole("textbox", { name: "Default monthly cap" }),
    ).not.toBeInTheDocument();
  });

  it("turns budgets off", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.click(
      await screen.findByRole("button", { name: "Turn off budgets" }),
    );

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["budgets"],
      undefined,
    );
  });

  it("edits model prices and removes them when every price is cleared", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(
      await screen.findByRole("textbox", {
        name: "local input price",
      }),
      { target: { value: "0.5" } },
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["models", "local", "pricing"],
      {
        input: 0.5,
      },
    );

    fireEvent.change(
      screen.getByRole("textbox", { name: "astra cache read price" }),
      {
        target: { value: "0.5" },
      },
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["models", "astra", "pricing"],
      {
        input: 5,
        output: 30,
        cache_read: 0.5,
      },
    );

    fireEvent.change(
      screen.getByRole("textbox", { name: "luna input price" }),
      {
        target: { value: "" },
      },
    );
    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["models", "luna", "pricing"],
      {
        output: 1.25,
      },
    );
  });

  it("removes a model's pricing once its last price is cleared", async () => {
    setStore(
      budgetConfig({
        models: {
          luna: {
            provider: "openai",
            id: "gpt-6-luna",
            pricing: { input: 0.2 },
          },
        } as Config["models"],
      }),
    );
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    fireEvent.change(
      await screen.findByRole("textbox", { name: "luna input price" }),
      {
        target: { value: "" },
      },
    );

    expect(mockUpdateConfigValue).toHaveBeenLastCalledWith(
      ["models", "luna", "pricing"],
      undefined,
    );
  });

  it("warns about models with usage but no prices", async () => {
    vi.mocked(fetch).mockResolvedValue(respond(status));
    renderBudgets();

    expect(await screen.findByText(/Ollama qwen3\.8:27b/)).toBeInTheDocument();
    expect(screen.getByText(/12,345 tokens/)).toBeInTheDocument();
  });

  it("keeps settings editable when the runtime cannot report spend", async () => {
    vi.mocked(fetch).mockResolvedValue(
      respond({ detail: "Budget monitor unavailable" }, 503),
    );
    renderBudgets();

    expect(await screen.findByText(/Spend is unavailable/)).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: "Default monthly cap" }),
    ).toHaveValue("20");
  });

  it("saves the draft config and refreshes budget status", async () => {
    mockSaveConfig.mockResolvedValue({ status: "saved" });
    routeFetch();
    renderBudgets();

    fireEvent.click(await screen.findByRole("button", { name: "Save" }));

    expect(mockSaveConfig).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  });
});
