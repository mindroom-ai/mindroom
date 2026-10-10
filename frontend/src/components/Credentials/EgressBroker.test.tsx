import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EgressBroker } from "./EgressBroker";

const NOT_RUNNING =
  "Egress broker is not running. Set MINDROOM_EGRESS_BROKER_PORT and MINDROOM_EGRESS_BROKER_URL.";

let storeState: { agents: unknown[]; config: unknown };

vi.mock("@/store/configStore", () => ({
  useConfigStore: (selector: (state: typeof storeState) => unknown) =>
    selector(storeState),
}));

const agent = (id: string, tools: string[], extra = {}) => ({
  id,
  display_name: `Agent ${id}`,
  tools,
  ...extra,
});

const github = {
  name: "github",
  display_name: "GitHub",
  description: "GitHub API and git over HTTPS",
  configured: false,
  updated_at: null,
  active_source: null,
  key_configured: false,
  key_updated_at: null,
  oauth: null,
};
const openai = {
  name: "openai",
  display_name: null,
  description: "OpenAI API",
  configured: true,
  updated_at: "2026-10-05T12:00:00+00:00",
  active_source: "key",
  key_configured: true,
  key_updated_at: "2026-10-05T12:00:00+00:00",
  oauth: null,
};
const githubAccount = {
  provider: "github",
  display_name: "GitHub",
  connected: false,
  account_label: null,
  can_connect: true,
  reset_required: false,
  service_account: false,
  unavailable_reason: null,
  shared_worker_opt_in: false,
};
const authorization = {
  provider: "github",
  auth_url: "https://github.com/login/oauth/authorize",
  completion_origin: "https://portal.example.com",
};

const newest = {
  at: "2026-10-09T10:30:00+00:00",
  kind: "request",
  scope: "shared",
  agent_name: "coder",
  requester_id: null,
  method: "POST",
  host: "api.github.com",
  path: "/repos/acme/app/issues",
  service: "github",
  status: 201,
  bytes_up: 12,
  bytes_down: 34,
  duration_ms: 87,
};
const oldest = {
  ...newest,
  at: "2026-10-09T10:00:00+00:00",
  agent_name: null,
  method: "GET",
  host: "example.com",
  path: "/",
  service: null,
  status: 403,
  duration_ms: 3,
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

let services: unknown[];
let logsResponse: () => Response | Promise<Response>;
let mutationResponse: () => Response;

function mockApi() {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/egress-broker/services" && method === "GET")
      return json({ services });
    if (url.pathname === "/api/egress-broker/logs") return logsResponse();
    return mutationResponse();
  });
}

function requests(pathname: string, method = "GET") {
  return vi
    .mocked(fetch)
    .mock.calls.filter(
      ([input, init]) =>
        new URL(String(input), "http://localhost").pathname === pathname &&
        (init?.method ?? "GET") === method,
    )
    .map(([input]) => new URL(String(input), "http://localhost"));
}

const row = (name: string) => screen.getByRole("listitem", { name });
const selectTarget = (value: string) =>
  fireEvent.change(screen.getByLabelText("Secrets for"), {
    target: { value },
  });

beforeEach(() => {
  storeState = {
    agents: [
      agent("coder", ["shell"], { include_default_tools: false }),
      agent("writer", ["file"], { include_default_tools: false }),
      agent("scripter", ["python"], { include_default_tools: false }),
      agent("inheriting", []),
      agent("isolated", [], { include_default_tools: false }),
    ],
    config: { defaults: { tools: ["shell"] } },
  };
  services = [github, openai];
  logsResponse = () => json({ records: [newest, oldest] });
  mutationResponse = () => new Response(null, { status: 204 });
  mockApi();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress broker panel", () => {
  it("lists the services with their status", async () => {
    render(<EgressBroker />);

    expect(await screen.findByText("GitHub")).toBeInTheDocument();
    expect(within(row("GitHub")).getByText("Not set")).toBeInTheDocument();
    expect(
      within(row("GitHub")).getByText("GitHub API and git over HTTPS"),
    ).toBeInTheDocument();
    // A service without a display name falls back to its key.
    expect(
      within(row("openai")).getByText(
        `Updated ${new Date(openai.updated_at).toLocaleDateString()}`,
      ),
    ).toBeInTheDocument();
    expect(
      within(row("openai")).getByRole("button", {
        name: "Replace openai API key",
      }),
    ).toBeInTheDocument();
    expect(requests("/api/egress-broker/services")[0].search).toBe("");
  });

  it("explains how to add services when none are configured", async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("/services")
        ? json({ services: [] })
        : logsResponse(),
    );
    render(<EgressBroker />);

    expect(
      await screen.findByText(/No egress services are configured/),
    ).toBeInTheDocument();
  });

  it("shows an error when the services cannot be loaded", async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("/services")
        ? json({ detail: "boom" }, 500)
        : logsResponse(),
    );
    render(<EgressBroker />);

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});

describe("egress broker secret target", () => {
  it("offers the global scope first, then agents that can run commands", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    const options = within(screen.getByLabelText("Secrets for"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    // `writer` and `isolated` have no shell or python of their own and opt out
    // of defaults.tools, which `inheriting` receives (shell).
    expect(options).toEqual([
      "Global (unscoped agents)",
      "Agent coder",
      "Agent scripter",
      "Agent inheriting",
    ]);
    expect(screen.getByLabelText("Secrets for")).toHaveValue("");
  });

  it("leaves out agents whose keys belong to each requester", async () => {
    storeState = {
      agents: [
        agent("coder", ["shell"]),
        agent("mine", ["shell"], { private: { per: "user_agent" } }),
        agent("per_user", ["shell"], { worker_scope: "user" }),
        agent("team", ["python"], { worker_scope: "shared" }),
        agent("unscoped", ["shell"], { worker_scope: null }),
      ],
      config: { defaults: { worker_scope: null } },
    };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    const options = within(screen.getByLabelText("Secrets for"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual([
      "Global (unscoped agents)",
      "Agent coder",
      "Agent team",
      "Agent unscoped",
    ]);
    expect(
      screen.getByText(/Agents with user or user_agent scope are not listed/),
    ).toBeInTheDocument();
  });

  it("follows the default worker scope for agents without their own", async () => {
    storeState = {
      agents: [
        agent("inherits", ["shell"]),
        agent("team", ["shell"], { worker_scope: "shared" }),
      ],
      config: { defaults: { worker_scope: "user_agent" } },
    };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    const options = within(screen.getByLabelText("Secrets for"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    expect(options).toEqual(["Global (unscoped agents)", "Agent team"]);
  });

  it("does not mention requester-scoped agents when there are none", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(
      screen.queryByText(/Agents with user or user_agent scope/),
    ).toBeNull();
  });

  it("only offers the global scope while the config is not loaded", async () => {
    storeState = { agents: [], config: null };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(
      within(screen.getByLabelText("Secrets for")).getAllByRole("option"),
    ).toHaveLength(1);
  });

  it("loads the services of the selected agent", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    selectTarget("coder");

    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );
    expect(
      requests("/api/egress-broker/services")[1].searchParams.get("agent_name"),
    ).toBe("coder");
  });

  it("saves a secret for the selected agent and clears the input", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    selectTarget("coder");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    const input = screen.getByLabelText("GitHub API key");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "ghp_secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        requests("/api/egress-broker/services/github/secret", "PUT"),
      ).toHaveLength(1),
    );
    const [put] = vi
      .mocked(fetch)
      .mock.calls.filter(([, init]) => init?.method === "PUT");
    expect(String(put[0])).toBe(
      "/api/egress-broker/services/github/secret?agent_name=coder",
    );
    expect(put[1]).toEqual(
      expect.objectContaining({
        body: JSON.stringify({ secret: "ghp_secret" }),
      }),
    );
    expect(screen.queryByLabelText("GitHub API key")).toBeNull();
    // The list is reloaded after a save.
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(3),
    );
    // Reopening the editor must not show the previous value.
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
  });

  it("saves a global secret without an agent_name", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "ghp_secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        requests("/api/egress-broker/services/github/secret", "PUT"),
      ).toHaveLength(1),
    );
    expect(
      requests("/api/egress-broker/services/github/secret", "PUT")[0].search,
    ).toBe("");
  });

  it("never carries a typed secret over to another target", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "global-only" },
    });
    selectTarget("coder");

    // The services reload for the new target, so nothing is left to submit.
    await waitFor(() =>
      expect(screen.queryByLabelText("GitHub API key")).toBeNull(),
    );
    await screen.findByText("GitHub");
    expect(screen.queryByDisplayValue("global-only")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
    expect(
      requests("/api/egress-broker/services/github/secret", "PUT"),
    ).toHaveLength(0);
  });

  it("does not show the previous target's services while the new ones load", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    selectTarget("coder");

    expect(screen.queryByText("GitHub")).toBeNull();
    expect(screen.getByText("Loading services...")).toBeInTheDocument();
  });

  it("falls back to the global scope when the selected agent goes away", async () => {
    const { rerender } = render(<EgressBroker />);
    await screen.findByText("GitHub");
    selectTarget("coder");
    await waitFor(() =>
      expect(screen.getByLabelText("Secrets for")).toHaveValue("coder"),
    );

    storeState = { ...storeState, agents: [agent("writer", ["file"])] };
    rerender(<EgressBroker />);

    expect(screen.getByLabelText("Secrets for")).toHaveValue("");
    await waitFor(() => {
      const loads = requests("/api/egress-broker/services");
      expect(loads).toHaveLength(3);
      expect(loads[2].searchParams.get("agent_name")).toBeNull();
    });
  });

  it("warns about shared keys only for the global scope", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(
      screen.getByRole("button", { name: "Remove openai API key" }),
    );
    expect(
      within(screen.getByRole("dialog")).getByText(
        "This deletes the global key, which every agent without a worker scope shares. Those agents lose access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Cancel",
      }),
    );

    selectTarget("coder");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Remove openai API key" }),
    );
    expect(
      within(screen.getByRole("dialog")).getByText(
        "This deletes the saved key. The agent loses access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
  });

  it("removes a secret after confirmation", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    selectTarget("coder");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Remove openai API key" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Remove",
      }),
    );

    await waitFor(() =>
      expect(
        requests("/api/egress-broker/services/openai/secret", "DELETE"),
      ).toHaveLength(1),
    );
    expect(
      requests(
        "/api/egress-broker/services/openai/secret",
        "DELETE",
      )[0].searchParams.get("agent_name"),
    ).toBe("coder");
  });

  it("explains a removed service and a forbidden save in dashboard terms", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    mutationResponse = () => json({ detail: "Service 'github' is gone" }, 404);
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "This service is no longer configured. Reload the page to update the list.",
      ),
    ).toBeInTheDocument();

    mutationResponse = () => json({ detail: "nope" }, 403);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText(
        "You are not allowed to change egress keys or accounts.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Connections/)).toBeNull();
  });
});

describe("egress broker connected accounts", () => {
  let popup: {
    closed: boolean;
    close: ReturnType<typeof vi.fn>;
    location: { href: string };
  };

  beforeEach(() => {
    popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    services = [{ ...github, oauth: githubAccount }, openai];
  });

  it("connects an account for the selected agent and reloads the list", async () => {
    mutationResponse = () => json(authorization);
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    selectTarget("coder");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub" }),
    );
    await waitFor(() =>
      expect(popup.location.href).toBe(authorization.auth_url),
    );
    const [connect] = requests(
      "/api/egress-broker/services/github/connect",
      "POST",
    );
    expect(connect.searchParams.get("agent_name")).toBe("coder");

    services = [
      {
        ...github,
        configured: true,
        active_source: "oauth",
        oauth: { ...githubAccount, connected: true, account_label: "octocat" },
      },
      openai,
    ];
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: authorization.completion_origin,
        source: popup as unknown as Window,
        data: {
          type: "mindroom:oauth-complete",
          provider: "github",
          status: "connected",
        },
      }),
    );
    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(requests("/api/egress-broker/services")).toHaveLength(3);
  });

  it("disconnects the global account without an agent_name", async () => {
    services = [
      {
        ...github,
        configured: true,
        active_source: "oauth",
        oauth: { ...githubAccount, connected: true, account_label: "octocat" },
      },
      openai,
    ];
    mutationResponse = () =>
      json({ status: "disconnected", provider: "github" });
    render(<EgressBroker />);
    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Disconnect GitHub" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Disconnect",
      }),
    );

    await waitFor(() =>
      expect(
        requests("/api/egress-broker/services/github/disconnect", "POST"),
      ).toHaveLength(1),
    );
    expect(
      requests("/api/egress-broker/services/github/disconnect", "POST")[0]
        .search,
    ).toBe("");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );
  });
});

describe("egress broker request log", () => {
  it("preserves the order the server returns, with local times", async () => {
    render(<EgressBroker />);

    const table = await screen.findByRole("table", { name: "Request log" });
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[0])
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual([
      "Time",
      "Agent",
      "Method",
      "Host",
      "Path",
      "Service",
      "Status",
      "Duration",
    ]);
    expect(
      within(rows[1])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      new Date(newest.at).toLocaleString(),
      "coder",
      "POST",
      "api.github.com",
      "/repos/acme/app/issues",
      "github",
      "201",
      "87 ms",
    ]);
    expect(
      within(rows[2])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      new Date(oldest.at).toLocaleString(),
      "-",
      "GET",
      "example.com",
      "/",
      "-",
      "403",
      "3 ms",
    ]);
  });

  it("says so when no requests are logged", async () => {
    logsResponse = () => json({ records: [] });
    render(<EgressBroker />);

    expect(
      await screen.findByText("No requests logged yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("filters the log by agent, host and service on refresh", async () => {
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });
    expect(requests("/api/egress-broker/logs")[0].search).toBe("");

    fireEvent.change(screen.getByLabelText("Filter by agent"), {
      target: { value: "coder" },
    });
    fireEvent.change(screen.getByLabelText("Filter by host"), {
      target: { value: "api.github.com" },
    });
    fireEvent.change(screen.getByLabelText("Filter by service"), {
      target: { value: "github" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() =>
      expect(requests("/api/egress-broker/logs")).toHaveLength(2),
    );
    const { searchParams } = requests("/api/egress-broker/logs")[1];
    expect(searchParams.get("agent_name")).toBe("coder");
    expect(searchParams.get("host")).toBe("api.github.com");
    expect(searchParams.get("service")).toBe("github");
  });

  it("keeps the log filter independent of the secret target", async () => {
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });

    selectTarget("coder");
    await waitFor(() =>
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );

    expect(screen.getByLabelText("Filter by agent")).toHaveValue("");
    expect(requests("/api/egress-broker/logs")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() =>
      expect(requests("/api/egress-broker/logs")).toHaveLength(2),
    );
    expect(
      requests("/api/egress-broker/logs")[1].searchParams.get("agent_name"),
    ).toBeNull();
  });

  it("replaces the log table with the not-running message on 409", async () => {
    logsResponse = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressBroker />);

    expect(await screen.findByText(NOT_RUNNING)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    // Services stay manageable while the broker is down.
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("shows a generic error when the log request fails", async () => {
    logsResponse = () => json({ detail: "boom" }, 500);
    render(<EgressBroker />);

    expect(
      await screen.findByText("Could not load the request log. Try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText(NOT_RUNNING)).toBeNull();
  });

  it("gives up on a hung log request so Refresh works again", async () => {
    // Testing Library's async helpers wait on real timers, so capture the
    // 30 second timeouts instead of faking time.
    const realSetTimeout = globalThis.setTimeout;
    const timeouts: Array<() => void> = [];
    vi.spyOn(globalThis, "setTimeout").mockImplementation(((
      callback: () => void,
      delay?: number,
    ) => {
      if (delay !== 30_000) return realSetTimeout(callback, delay);
      timeouts.push(callback);
      return 0;
    }) as unknown as typeof setTimeout);
    vi.mocked(fetch).mockImplementation(async (input, init) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/egress-broker/services")
        return json({ services: [github] });
      return new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      });
    });
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();

    await act(async () => {
      timeouts.forEach((fire) => fire());
    });

    expect(
      await screen.findByText("Could not load the request log. Try again."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("recovers once the broker is running and the log is refreshed", async () => {
    logsResponse = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressBroker />);
    await screen.findByText(NOT_RUNNING);

    logsResponse = () => json({ records: [newest] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(
      await screen.findByRole("table", { name: "Request log" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(NOT_RUNNING)).toBeNull();
  });
});

describe("egress broker CA certificate", () => {
  it("links to the CA certificate download", async () => {
    render(<EgressBroker />);

    const link = await screen.findByRole("link", {
      name: "Download CA certificate",
    });
    expect(link).toHaveAttribute("href", "/api/egress-broker/ca.pem");
    expect(link).toHaveAttribute("download");
  });

  it("disables the download while the broker is not running", async () => {
    logsResponse = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressBroker />);
    await screen.findByText(NOT_RUNNING);

    expect(screen.queryByRole("link")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Download CA certificate" }),
    ).toBeDisabled();

    logsResponse = () => json({ records: [] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(
      await screen.findByRole("link", { name: "Download CA certificate" }),
    ).toBeInTheDocument();
  });
});
