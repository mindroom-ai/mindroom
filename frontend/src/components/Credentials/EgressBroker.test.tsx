import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  type Mock,
  vi,
} from "vitest";
import egressPresets from "@/test/fixtures/egressPresets.json";
import {
  credentialService,
  expectNoAction,
  oauthStatus,
  presetsReady,
  pressInDialog,
  rowAction,
} from "@/test/egressServiceTestKit";
import { EgressBroker } from "./EgressBroker";

const NOT_RUNNING =
  "Egress broker is not running. Set MINDROOM_EGRESS_BROKER_PORT and MINDROOM_EGRESS_BROKER_URL.";

let storeState: {
  agents: unknown[];
  config: unknown;
  isDirty?: boolean;
  dirtyRoots?: string[];
  draftVersion?: number;
  updateConfigValue?: (path: string[], value: unknown) => void;
  restoreConfigValue?: (
    path: string[],
    value: unknown,
    before: { isDirty: boolean; dirtyRoots: string[]; draftVersion: number },
  ) => void;
  saveConfig?: () => Promise<unknown>;
};

vi.mock("@/store/configStore", () => {
  // The real store always has dirtyRoots, which most states here leave out.
  const state = () => ({ dirtyRoots: [], ...storeState });
  return {
    // The panel reads what the draft is when a write starts straight from the store.
    useConfigStore: Object.assign(
      (selector: (state: typeof storeState) => unknown) => selector(state()),
      { getState: state },
    ),
  };
});

const agent = (id: string, tools: string[], extra = {}) => ({
  id,
  display_name: `Agent ${id}`,
  tools,
  ...extra,
});

// The dashboard listing has no is_shared or can_manage; the panel sets both over these.
const {
  is_shared: _isShared,
  can_manage: _canManage,
  ...github
} = credentialService();
const openai = {
  name: "openai",
  display_name: null,
  description: "OpenAI API",
  source: "config",
  rules: [],
  restrict_only: false,
  configured: true,
  active_source: "key",
  key_configured: true,
  key_updated_at: "2026-10-05T12:00:00+00:00",
  oauth: null,
};
const githubAccount = oauthStatus();
const connectedGithub = {
  ...github,
  configured: true,
  active_source: "oauth",
  oauth: { ...githubAccount, connected: true, account_label: "octocat" },
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
  requester_id: "@bob:example.com",
  method: "POST",
  host: "api.github.com",
  path: "/repos/acme/app/issues",
  service: "github",
  status: 201,
  bytes_up: 12,
  bytes_down: 34,
  duration_ms: 87,
  code: null,
};
const oldest = {
  ...newest,
  at: "2026-10-09T10:00:00+00:00",
  agent_name: null,
  requester_id: null,
  method: "GET",
  host: "example.com",
  path: "/",
  service: null,
  status: 403,
  duration_ms: 3,
  kind: "denied",
  code: "host_not_allowed",
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

let services: unknown[];
// The agents whose keys the panel sets, which every services listing names.
// Unless a test says otherwise, no agent has per-user keys and their page is served.
let keyAgents: {
  shared_agents: string[];
  has_unscoped_agents: boolean;
  has_personal_agents?: boolean;
  personal_page_served?: boolean;
};
let logsResponse: () => Response | Promise<Response>;
let mutationResponse: () => Response;

function mockApi() {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/egress-broker/services" && method === "GET")
      return json({
        services,
        has_personal_agents: false,
        personal_page_served: true,
        ...keyAgents,
      });
    if (url.pathname === "/api/egress-broker/logs") return logsResponse();
    if (url.pathname === "/api/egress-broker/presets")
      return json(egressPresets);
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

// The panel's first listing checks that the broker is on, and the agents
// without a scope start from it when they are shown first; the loads come after it.
const listings = () => requests("/api/egress-broker/services").slice(1);

const row = (name: string) => screen.getByRole("row", { name });
const selectTarget = (value: string) =>
  fireEvent.change(screen.getByLabelText("Keys for"), {
    target: { value },
  });
// The panel starts on the first shared agent; "" is Agents without a scope.
function sharedCoder() {
  keyAgents = { shared_agents: ["coder"], has_unscoped_agents: true };
  storeState = {
    agents: [
      agent("coder", ["shell"], { worker_scope: "shared" }),
      agent("scripter", ["python"]),
    ],
    config: { defaults: {} },
  };
}

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
  keyAgents = { shared_agents: [], has_unscoped_agents: true };
  logsResponse = () => json({ records: [newest, oldest] });
  mutationResponse = () => new Response(null, { status: 204 });
  mockApi();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("whether the panel shows", () => {
  it("shows nothing and asks nothing more while the broker is off", async () => {
    // Every egress route answers 404 while the broker is off.
    vi.mocked(fetch).mockImplementation(async () =>
      json({ detail: "Agent Keys are not enabled" }, 404),
    );
    const { container } = render(<EgressBroker />);

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    // Give a follow-up request, or a card, the chance to appear.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(container).toBeEmptyDOMElement();
    expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input))).toEqual(
      ["/api/egress-broker/services"],
    );
  });

  it("shows nothing and asks nothing more when the check itself fails", async () => {
    // Only a listing tells that the broker is on; a failure tells nothing.
    const failures: Array<() => Promise<Response>> = [
      () => Promise.reject(new Error("Network down")),
      async () => json({ detail: "Not authenticated" }, 401),
      async () => json({ detail: "boom" }, 500),
    ];
    for (const failure of failures) {
      vi.mocked(fetch).mockClear();
      vi.mocked(fetch).mockImplementation(failure);
      const { container, unmount } = render(<EgressBroker />);

      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
      // Give a follow-up request, or a card, the chance to appear.
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 20));
      });
      expect(container).toBeEmptyDOMElement();
      expect(fetch).toHaveBeenCalledTimes(1);
      unmount();
    }
  });

  it("starts the keys of the agents without a scope from the check's listing, without reading it again", async () => {
    render(<EgressBroker />);
    expect(await screen.findByText("GitHub")).toBeInTheDocument();
    // Give a second listing the chance to go out.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });

    expect(
      requests("/api/egress-broker/services").map((url) => url.search),
    ).toEqual([""]);
  });

  it("gives up on a hung check and shows nothing", async () => {
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
      if (
        new URL(String(input), "http://localhost").pathname !==
        "/api/egress-broker/services"
      )
        return logsResponse();
      return new Promise<Response>((_, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      });
    });
    const { container } = render(<EgressBroker />);
    await waitFor(() => expect(timeouts).toHaveLength(1));
    expect(container).toBeEmptyDOMElement();

    await act(async () => {
      timeouts.forEach((fire) => fire());
    });
    // Give a follow-up request, or a card, the chance to appear.
    await act(async () => {
      await new Promise((resolve) => realSetTimeout(resolve, 20));
    });

    expect(container).toBeEmptyDOMElement();
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("cancels the check when the panel goes away", async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementation((_input, init) => {
      signal = init?.signal ?? undefined;
      return new Promise<Response>(() => undefined);
    });
    const { unmount } = render(<EgressBroker />);
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
  });
});

describe("egress broker panel", () => {
  it("is titled Agent Keys", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(
      screen.getByRole("heading", { name: "Agent Keys" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Egress broker")).toBeNull();
  });

  it("says what Add service offers and links the page where each user sets per-user keys", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    const note = screen.getByText(/Services you add here are offered/);
    expect(note.textContent).toBe(
      "Services you add here are offered to every agent and user. Agents with a user or user_agent worker scope and private agents are not listed: each user sets their keys on the Connections page's Agent Keys page.",
    );
    expect(
      within(note).getByRole("link", { name: "Agent Keys page" }),
    ).toHaveAttribute("href", "/connections/egress");
    // It sits under the Add service button, whatever the agents are.
    expect(
      screen
        .getByRole("button", { name: "Add service" })
        .compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("says nobody can sign in to the Agent Keys page while Chat sign-in and trusted upstream JWT are both off", async () => {
    keyAgents = { ...keyAgents, personal_page_served: false };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    const note = screen.getByText(/Services you add here are offered/);
    expect(note.textContent).toBe(
      "Services you add here are offered to every agent and user. Agents with a user or user_agent worker scope and private agents are not listed: each user sets their keys on the Connections page's Agent Keys page, which nobody can sign in to while sign-in from MindRoom Chat is off and trusted upstream JWT auth is not set. Give such agents a shared worker scope to set their keys here.",
    );
    expect(within(note).queryByRole("link")).toBeNull();
  });

  it("repeats the note for every target", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    expect(
      screen.getAllByRole("link", { name: "Agent Keys page" }),
    ).toHaveLength(1);

    selectTarget("");
    await screen.findByText("GitHub");
    expect(
      screen.getAllByRole("link", { name: "Agent Keys page" }),
    ).toHaveLength(1);
  });

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
        `Key saved ${new Date(openai.key_updated_at).toLocaleDateString()}`,
      ),
    ).toBeInTheDocument();
    expect(
      await rowAction("Replace key for openai", row("openai")),
    ).toBeInTheDocument();
    expect(
      requests("/api/egress-broker/services").map((url) => url.search),
    ).toEqual([""]);
  });

  it("shows an error when the services cannot be loaded", async () => {
    // The check's listing shows the panel; the shared agent's own listing fails.
    sharedCoder();
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("agent_name=")
        ? json({ detail: "boom" }, 500)
        : String(input).includes("/services")
          ? json({ services, ...keyAgents })
          : logsResponse(),
    );
    render(<EgressBroker />);

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});

describe("egress broker key target", () => {
  const optionsOf = () =>
    within(screen.getByLabelText("Keys for"))
      .queryAllByRole("option")
      .map((option) => option.textContent);

  // The three agents the target tests name, labelled from the store.
  const teamAgents = () => ({
    agents: [
      agent("coder", ["shell"]),
      agent("team", ["python"], { worker_scope: "shared" }),
      agent("ops", ["shell"], { worker_scope: "shared" }),
    ],
    config: { defaults: {} },
  });

  it("lists the shared agents the listing names first, then Agents without a scope", async () => {
    keyAgents = { shared_agents: ["team", "ops"], has_unscoped_agents: true };
    storeState = teamAgents();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(optionsOf()).toEqual([
      "Agent team",
      "Agent ops",
      "Agents without a scope",
    ]);
  });

  it("offers a shared agent whose shell comes from a tool preset", async () => {
    // The backend resolves the preset, which includes `shell`, so it names this agent.
    keyAgents = { shared_agents: ["team"], has_unscoped_agents: false };
    storeState = {
      agents: [
        agent("team", ["openclaw_compat"], {
          worker_scope: "shared",
          include_default_tools: false,
        }),
      ],
      config: { defaults: {} },
    };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(optionsOf()).toEqual(["Agent team"]);
    expect(screen.getByLabelText("Keys for")).toBeEnabled();
    expect(screen.getByLabelText("Keys for")).toHaveValue("team");
    expect(listings()[0].searchParams.get("agent_name")).toBe("team");
    expect(screen.queryByText("No agent uses these keys.")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Set key for GitHub" }),
    ).toBeVisible();
  });

  it("defaults to the first shared agent and loads its keys", async () => {
    keyAgents = { shared_agents: ["team", "ops"], has_unscoped_agents: true };
    storeState = teamAgents();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(screen.getByLabelText("Keys for")).toHaveValue("team");
    const loads = listings();
    expect(loads).toHaveLength(1);
    expect(loads[0].searchParams.get("agent_name")).toBe("team");
  });

  it("offers Agents without a scope only when the listing says one uses the broker", async () => {
    keyAgents = { shared_agents: ["team"], has_unscoped_agents: false };
    storeState = teamAgents();
    const { unmount } = render(<EgressBroker />);
    await screen.findByText("GitHub");
    expect(optionsOf()).toEqual(["Agent team"]);
    unmount();

    // Only agents without a scope: the one option is the default and uses the
    // keys that all of them share, so no agent is named.
    keyAgents = { shared_agents: [], has_unscoped_agents: true };
    vi.mocked(fetch).mockClear();
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    expect(optionsOf()).toEqual(["Agents without a scope"]);
    expect(screen.getByLabelText("Keys for")).toHaveValue("");
    expect(
      requests("/api/egress-broker/services").map((url) =>
        url.searchParams.get("agent_name"),
      ),
    ).toEqual([null]);
  });

  it("names a shared agent by its id while the config is not loaded", async () => {
    keyAgents = { shared_agents: ["team"], has_unscoped_agents: false };
    storeState = { agents: [], config: null };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(optionsOf()).toEqual(["team"]);
  });

  it("says that no agent uses these keys only when the listing names no shared agent and none without a scope", async () => {
    storeState = { agents: [], config: { defaults: {} } };
    const cases: Array<[string[], boolean, boolean]> = [
      [["team"], false, false],
      [[], true, false],
      [[], false, true],
    ];
    for (const [sharedAgents, hasUnscopedAgents, unused] of cases) {
      keyAgents = {
        shared_agents: sharedAgents,
        has_unscoped_agents: hasUnscopedAgents,
      };
      const { unmount } = render(<EgressBroker />);
      await screen.findByText("GitHub");
      expect(screen.queryByText("No agent uses these keys.") !== null).toBe(
        unused,
      );
      unmount();
    }
  });

  it("hides the key actions and says so when no agent uses these keys", async () => {
    // No agent runs commands, so none uses the broker.
    keyAgents = { shared_agents: [], has_unscoped_agents: false };
    storeState = {
      agents: [agent("notes", ["calculator"], { worker_scope: "user" })],
      config: { defaults: {} },
    };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(screen.getByLabelText("Keys for")).toBeDisabled();
    expect(screen.getByText("No agent uses these keys.")).toBeVisible();
    for (const name of [
      "Set key for GitHub",
      "Replace key for openai",
      "Remove key for openai",
    ])
      await expectNoAction(name);
    // What changes the service, not its keys, stays.
    expect(await rowAction("Edit service for GitHub")).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("hides the connect and disconnect actions of an account too", async () => {
    keyAgents = { shared_agents: [], has_unscoped_agents: false };
    storeState = {
      agents: [agent("mine", ["shell"], { worker_scope: "user" })],
      config: { defaults: {} },
    };
    services = [
      {
        ...github,
        source: "config",
        rules: [],
        oauth: { ...githubAccount, connected: true },
        configured: true,
        active_source: "oauth",
      },
      {
        ...openai,
        name: "gitlab",
        display_name: "GitLab",
        key_configured: false,
        configured: false,
        active_source: null,
        source: "config",
        rules: [],
        oauth: { ...githubAccount, provider: "gitlab", display_name: "GitLab" },
      },
    ];
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(screen.getByText("No agent uses these keys.")).toBeVisible();
    await expectNoAction(/Disconnect/);
    expect(screen.queryByRole("button", { name: /Connect GitLab/ })).toBeNull();
    await expectNoAction(/Use an API key instead/);
  });

  it("keeps the key actions while an agent uses the keys", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(screen.queryByText("No agent uses these keys.")).toBeNull();
    expect(
      screen.getByRole("button", { name: "Set key for GitHub" }),
    ).toBeVisible();
  });

  it("says that only agents with per-user keys use the broker, not that no agent does", async () => {
    // Every command agent keeps keys per requester, so none is named.
    keyAgents = {
      shared_agents: [],
      has_unscoped_agents: false,
      has_personal_agents: true,
    };
    storeState = {
      agents: [agent("mine", ["shell"], { worker_scope: "user" })],
      config: { defaults: {} },
    };
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    expect(screen.queryByText("No agent uses these keys.")).toBeNull();
    expect(
      screen.getByText(
        "Only agents whose keys each user sets use the broker, so no keys are set here.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Keys for")).toBeDisabled();
    await expectNoAction("Set key for GitHub");
  });

  it("loads the services of the selected target", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    expect(listings()[0].searchParams.get("agent_name")).toBe("coder");

    selectTarget("");
    await waitFor(() => expect(listings()).toHaveLength(2));
    expect(listings()[1].searchParams.get("agent_name")).toBeNull();

    selectTarget("coder");
    await waitFor(() => expect(listings()).toHaveLength(3));
    expect(listings()[2].searchParams.get("agent_name")).toBe("coder");
  });

  it("saves a secret for the selected agent and clears the input", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
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
    await waitFor(() => expect(listings()).toHaveLength(2));
    // Reopening the editor must not show the previous value.
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
  });

  it("saves a global secret without an agent_name", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
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
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "coder-only" },
    });
    selectTarget("");

    // The services reload for the new target, so nothing is left to submit.
    await waitFor(() =>
      expect(screen.queryByLabelText("GitHub API key")).toBeNull(),
    );
    await screen.findByText("GitHub");
    expect(screen.queryByDisplayValue("coder-only")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
    expect(
      requests("/api/egress-broker/services/github/secret", "PUT"),
    ).toHaveLength(0);
  });

  it("does not show the previous target's services while the new ones load", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => undefined),
    );

    selectTarget("");

    expect(screen.queryByText("GitHub")).toBeNull();
    expect(screen.getByText("Loading services...")).toBeInTheDocument();
  });

  it("falls back to the first target when a later listing no longer names the selected one", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");
    selectTarget("");
    await waitFor(() => expect(listings()).toHaveLength(2));
    await screen.findByText("GitHub");

    // No agent without a scope is left, which the reload after a saved key says.
    keyAgents = { shared_agents: ["coder"], has_unscoped_agents: false };
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "ghp_secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(screen.getByLabelText("Keys for")).toHaveValue("coder"),
    );
    expect(optionsOf()).toEqual(["Agent coder"]);
    await waitFor(() => {
      const loads = listings();
      expect(loads).toHaveLength(4);
      expect(loads[2].searchParams.get("agent_name")).toBeNull();
      expect(loads[3].searchParams.get("agent_name")).toBe("coder");
    });
  });

  it("warns that a shared agent's key is shared, and the global key most of all", async () => {
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(await rowAction("Remove key for openai"));
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

    sharedCoder();
    cleanup();
    render(<EgressBroker />);
    fireEvent.click(await rowAction("Remove key for openai"));
    expect(
      within(screen.getByRole("dialog")).getByText(
        "This deletes the shared key. Everyone who relies on it loses access to this service until a key is set again.",
      ),
    ).toBeInTheDocument();
  });

  it("removes a secret after confirmation", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(await rowAction("Remove key for openai"));
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
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
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
    const forbidden = await screen.findByText(
      "You are not allowed to change egress keys or accounts.",
    );
    expect(forbidden.textContent).not.toMatch(/Connections/);
  });
});

describe("egress broker connected accounts", () => {
  let popup: {
    closed: boolean;
    close: ReturnType<typeof vi.fn>;
    location: { href: string };
    document: Document;
  };

  beforeEach(() => {
    popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
      document: document.implementation.createHTMLDocument(),
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    services = [{ ...github, oauth: githubAccount }, openai];
  });

  it("connects an account for the selected agent and reloads the list", async () => {
    mutationResponse = () => json(authorization);
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByText("GitHub");

    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub for GitHub" }),
    );
    // Everyone who uses the shared agent acts as the account, so the admin is asked first.
    const question = await screen.findByRole("dialog", {
      name: "Connect a shared account?",
    });
    expect(
      within(question).getByText(
        "Everyone who uses Agent coder will act as the GitHub account you pick. Use a team account, not your personal one.",
      ),
    ).toBeVisible();
    expect(popup.location.href).toBe("about:blank");
    fireEvent.click(within(question).getByRole("button", { name: "Continue" }));
    await waitFor(() =>
      expect(popup.location.href).toBe(authorization.auth_url),
    );
    const [connect] = requests(
      "/api/egress-broker/services/github/connect",
      "POST",
    );
    expect(connect.searchParams.get("agent_name")).toBe("coder");

    services = [connectedGithub, openai];
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: authorization.completion_origin,
        source: popup as unknown as Window,
        data: {
          type: "mindroom-oauth",
          provider: "github",
          status: "connected",
        },
      }),
    );
    expect(
      await screen.findByText("Shared with everyone using Agent coder"),
    ).toBeInTheDocument();
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(listings()).toHaveLength(2);
  });

  it("reads the selected agent's services again when the popup closes without answering", async () => {
    mutationResponse = () => json(authorization);
    sharedCoder();
    render(<EgressBroker />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub for GitHub" }),
    );
    const question = await screen.findByRole("dialog", {
      name: "Connect a shared account?",
    });
    fireEvent.click(within(question).getByRole("button", { name: "Continue" }));
    await waitFor(() =>
      expect(popup.location.href).toBe(authorization.auth_url),
    );

    // The connection finished, but its message never arrived.
    services = [connectedGithub, openai];
    popup.closed = true;

    expect(
      await screen.findByText(
        "Shared with everyone using Agent coder",
        {},
        { timeout: 2000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("octocat")).toBeInTheDocument();
    expect(screen.queryByText("Authorization was cancelled.")).toBeNull();
    // The first load, the check after the popup closed, and the reload.
    const loads = listings();
    expect(loads).toHaveLength(3);
    expect(loads.map((url) => url.searchParams.get("agent_name"))).toEqual([
      "coder",
      "coder",
      "coder",
    ]);
  });

  it("disconnects the global account without an agent_name", async () => {
    services = [connectedGithub, openai];
    mutationResponse = () =>
      json({ status: "disconnected", provider: "github" });
    render(<EgressBroker />);
    expect(
      await screen.findByText(
        "Shared with everyone using the agents without a scope",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("octocat")).toBeInTheDocument();

    fireEvent.click(await rowAction("Disconnect for GitHub"));
    const dialog = await screen.findByRole("dialog", {
      name: "Disconnect GitHub?",
    });
    expect(
      within(dialog).getByText(
        "This removes the saved GitHub connection for the agents without a scope. These services stop working until an account is connected again: GitHub.",
      ),
    ).toBeVisible();
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));

    await waitFor(() =>
      expect(
        requests("/api/egress-broker/services/github/disconnect", "POST"),
      ).toHaveLength(1),
    );
    expect(
      requests("/api/egress-broker/services/github/disconnect", "POST")[0]
        .search,
    ).toBe("");
    await waitFor(() => expect(listings()).toHaveLength(1));
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
      "User",
      "Agent",
      "Method",
      "Host",
      "Status",
      "Service",
      "Path",
    ]);
    expect(
      within(rows[1])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      new Date(newest.at).toLocaleString(),
      "@bob:example.com",
      // The agent is named as the config names it, not by its id.
      "Agent coder",
      "POST",
      "api.github.com",
      "201",
      "github",
      "/repos/acme/app/issues",
    ]);
    expect(
      within(rows[2])
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      new Date(oldest.at).toLocaleString(),
      "-",
      "-",
      "GET",
      "example.com",
      // The refusal code sits beside the status.
      "403host_not_allowed",
      "-",
      "/",
    ]);
    expect(within(rows[1]).queryByText("host_not_allowed")).toBeNull();
  });

  it("keeps the duration in the time's title and cuts a long path short with its full value as the title", async () => {
    const path = `/repos/acme/app/${"very-long-segment/".repeat(12)}issues`;
    logsResponse = () => json({ records: [{ ...newest, path }] });
    render(<EgressBroker />);

    const table = await screen.findByRole("table", { name: "Request log" });
    const cells = within(within(table).getAllByRole("row")[1]).getAllByRole(
      "cell",
    );
    expect(cells[0]).toHaveAttribute("title", "87 ms");
    expect(within(table).queryByText("87 ms")).toBeNull();
    const pathCell = cells[cells.length - 1];
    expect(pathCell).toHaveAttribute("title", path);
    expect(pathCell).toHaveClass("truncate");
  });

  it("names an agent the config does not know by its id", async () => {
    logsResponse = () =>
      json({ records: [{ ...newest, agent_name: "retired_agent" }] });
    render(<EgressBroker />);

    const table = await screen.findByRole("table", { name: "Request log" });
    expect(within(table).getByText("retired_agent")).toBeInTheDocument();
  });

  it("offers the user filter without a value until one is typed", async () => {
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });

    expect(screen.getByLabelText("Filter by user")).toHaveValue("");
    expect(
      requests("/api/egress-broker/logs")[0].searchParams.has("requester"),
    ).toBe(false);
  });

  it("shows refusal codes in full and lets them wrap instead of clipping", async () => {
    const code = "credential_not_configured";
    logsResponse = () => json({ records: [{ ...oldest, status: 403, code }] });
    render(<EgressBroker />);

    const table = await screen.findByRole("table", { name: "Request log" });
    const shown = within(table).getByText(code);
    expect(shown.textContent).toBe(code);
    // Nothing from the code up to the table truncates, hides overflow, or
    // forbids wrapping, so a narrow Status column wraps the code.
    expect(shown).toHaveClass("break-words");
    for (
      let node: HTMLElement | null = shown;
      node !== null && node !== table;
      node = node.parentElement
    ) {
      expect(node.className).not.toMatch(
        /\b(truncate|overflow-hidden|text-ellipsis|whitespace-nowrap)\b/,
      );
    }
    // It wraps after an underscore, not inside a word.
    expect(
      Array.from(shown.childNodes)
        .map((node) => (node.nodeName === "WBR" ? "|" : node.textContent))
        .join(""),
    ).toBe("credential_|not_|configured");
  });

  it("says so when no requests are logged", async () => {
    logsResponse = () => json({ records: [] });
    render(<EgressBroker />);

    expect(
      await screen.findByText("No requests logged yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Request log" })).toBeNull();
  });

  it("filters the log by user, agent, host and service on refresh", async () => {
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });
    expect(requests("/api/egress-broker/logs")[0].search).toBe("");

    fireEvent.change(screen.getByLabelText("Filter by user"), {
      target: { value: " @bob:example.com " },
    });
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
    // The admin API calls the user filter `requester`.
    expect(searchParams.get("requester")).toBe("@bob:example.com");
    expect(searchParams.has("user")).toBe(false);
    expect(searchParams.get("agent_name")).toBe("coder");
    expect(searchParams.get("host")).toBe("api.github.com");
    expect(searchParams.get("service")).toBe("github");
  });

  it("filters the log by an agent's display name, as the table shows it", async () => {
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });

    fireEvent.change(screen.getByLabelText("Filter by agent"), {
      target: { value: " Agent coder " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() =>
      expect(requests("/api/egress-broker/logs")).toHaveLength(2),
    );
    expect(
      requests("/api/egress-broker/logs")[1].searchParams.get("agent_name"),
    ).toBe("coder");
  });

  it("keeps the log filter independent of the secret target", async () => {
    sharedCoder();
    render(<EgressBroker />);
    await screen.findByRole("table", { name: "Request log" });

    selectTarget("");
    await waitFor(() => expect(listings()).toHaveLength(2));

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
    expect(screen.queryByRole("table", { name: "Request log" })).toBeNull();
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
        return json({ services: [github], ...keyAgents });
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

    expect(
      screen.queryByRole("link", { name: "Download CA certificate" }),
    ).toBeNull();
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

describe("egress broker service editor", () => {
  const githubPreset = { preset: "github" };
  const configRow = (
    name: string,
    display_name: string,
    rules: { host: string; port: number | null; path_prefix: string }[] = [],
  ) => ({
    ...github,
    name,
    display_name,
    source: "config",
    rules,
  });
  const githubRules = [
    { host: "api.github.com", port: null, path_prefix: "/" },
    { host: "uploads.github.com", port: null, path_prefix: "/" },
    { host: "github.com", port: null, path_prefix: "/" },
  ];
  const clean = { isDirty: false, dirtyRoots: [] as string[], draftVersion: 0 };
  let updateConfigValue: Mock<(path: string[], value: unknown) => void>;
  let restoreConfigValue: Mock<
    (
      path: string[],
      value: unknown,
      before: { isDirty: boolean; dirtyRoots: string[]; draftVersion: number },
    ) => void
  >;
  let saveConfig: Mock<() => Promise<unknown>>;

  beforeEach(() => {
    updateConfigValue = vi.fn<(path: string[], value: unknown) => void>();
    restoreConfigValue = vi.fn();
    saveConfig = vi.fn<() => Promise<unknown>>(async () => ({
      status: "saved",
    }));
    storeState = {
      agents: [agent("coder", ["shell"], { include_default_tools: false })],
      config: {
        defaults: { tools: ["shell"] },
        egress_broker: {
          services: {
            github: githubPreset,
            openai: {
              rules: [{ host: "api.openai.com", auth: { type: "bearer" } }],
              oauth_on_shared_workers: true,
            },
          },
        },
      },
      ...clean,
      updateConfigValue,
      restoreConfigValue,
      saveConfig,
    };
    services = [
      configRow("github", "GitHub", githubRules),
      configRow("openai", "OpenAI", [
        { host: "api.openai.com", port: null, path_prefix: "/" },
      ]),
    ];
  });

  const fill = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  // Everything but the basic fields sits under More options.
  const openMoreOptions = () => {
    const toggle = screen.getByRole("button", { name: "More options" });
    if (toggle.getAttribute("aria-expanded") === "false")
      fireEvent.click(toggle);
  };
  const save = () =>
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
  const openAdd = async () => {
    render(<EgressBroker />);
    await screen.findByLabelText("GitHub");
    fireEvent.click(screen.getByRole("button", { name: "Add service" }));
    await presetsReady();
  };
  /** Opens Add service and fills in the OpenAI preset as myapi, ready to save. */
  const addMyapi = async () => {
    await openAdd();
    fill("Name", "myapi");
    fill("Preset", "openai");
  };
  const failSave = (message = "Failed to save configuration (Error 500)") =>
    saveConfig.mockResolvedValue({ status: "error", message, diagnostics: [] });
  const openEdit = async (name: string) => {
    render(<EgressBroker />);
    fireEvent.click(await rowAction(`Edit service for ${name}`));
    await presetsReady();
  };

  it("adds a service to the config through the store, in authored form", async () => {
    await openAdd();
    fill("Name", "myapi");
    fill("Preset", "anthropic");
    services = [...services, configRow("myapi", "Myapi")];
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledTimes(1);
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "myapi"],
      {
        preset: "anthropic",
      },
    );
    // The value is in the draft before the save, which is what Settings does too.
    expect(updateConfigValue.mock.invocationCallOrder[0]).toBeLessThan(
      saveConfig.mock.invocationCallOrder[0],
    );
    expect(await screen.findByLabelText("Myapi")).toBeInTheDocument();
    expect(screen.queryByRole("form")).toBeNull();
    expect(restoreConfigValue).not.toHaveBeenCalled();
    // Nothing goes to the personal API or a dashboard write route.
    expect(requests("/api/egress-broker/services", "PUT")).toHaveLength(0);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([input]) =>
          String(input).startsWith("/api/connections"),
        ),
    ).toBe(false);
    // The reload after the save.
    expect(listings()).toHaveLength(1);
  });

  it("loads the presets from the dashboard API when an editor opens, not before", async () => {
    render(<EgressBroker />);
    await screen.findByLabelText("GitHub");
    expect(requests("/api/egress-broker/presets")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Add service" }));
    await presetsReady();
    expect(requests("/api/egress-broker/presets")).toHaveLength(1);
    expect(
      within(screen.getByLabelText("Preset"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual(["Custom", ...egressPresets.presets.map((p) => p.display_name)]);
  });

  it("offers the account logins the presets endpoint lists, by display name", async () => {
    await openAdd();
    openMoreOptions();
    const login = screen.getByLabelText("Account login (optional)");
    expect(login.tagName).toBe("SELECT");
    expect(
      within(login)
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "No account login",
      ...egressPresets.oauth_providers.map((p) => p.display_name),
    ]);
  });

  it("saves a new custom service from a pasted address: its host as the display name and its path as the prefix", async () => {
    await openAdd();
    fill("Website", "https://httpbin.org/anything");
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "httpbin"],
      {
        display_name: "httpbin.org",
        rules: [
          {
            host: "httpbin.org",
            path_prefix: "/anything",
            auth: { type: "bearer" },
          },
        ],
      },
    );
  });

  // Only users add limits, so a limit here is one of the scope's own services, which users change on their page.
  it("shows a user's limit read-only, on its own row and on the administrator's service it narrows", async () => {
    services = [
      configRow("github", "GitHub", githubRules),
      {
        ...configRow("github-limited", "GitHub (acme/app)", [
          {
            host: "api.github.com",
            port: null,
            path_prefix: "/repos/acme/app",
          },
          { host: "github.com", port: null, path_prefix: "/acme/app" },
        ]),
        source: "user",
        restrict_only: true,
      },
    ];
    render(<EgressBroker />);

    const limit = await screen.findByLabelText("GitHub (acme/app)");
    expect(within(limit).getByText("Limit only")).toBeVisible();
    const githubRow = screen.getByLabelText("GitHub");
    expect(within(githubRow).getByText("Limited to acme/app")).toBeVisible();
    await expectNoAction(/Change limit|Remove limit/);
  });

  it("edits a preset service without expanding it", async () => {
    await openEdit("GitHub");
    const form = screen.getByRole("form", { name: "Edit github" });
    expect(within(form).getByLabelText("Preset")).toHaveValue("github");
    expect(within(form).queryByLabelText("Website")).toBeNull();
    openMoreOptions();
    fireEvent.click(
      within(form).getByLabelText("Refuse other paths on these hosts"),
    );
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "github"],
      {
        preset: "github",
        restrict_to_rules: true,
      },
    );
  });

  it("limits a GitHub service to a repository and saves the rules as authored", async () => {
    await openEdit("GitHub");
    fill("GitHub repositories", "basnijholt/agent-cli");
    fireEvent.click(
      screen.getByRole("button", { name: "Use these repositories" }),
    );
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    const [path, value] = updateConfigValue.mock.calls[0];
    const authored = value as {
      rules: { host: string; path_prefix: string }[];
    };
    expect(path).toEqual(["egress_broker", "services", "github"]);
    expect(authored).toMatchObject({
      preset: "github",
      restrict_to_rules: true,
    });
    expect(
      authored.rules.map((rule) => `${rule.host}${rule.path_prefix}`),
    ).toEqual([
      "api.github.com/repos/basnijholt/agent-cli",
      "github.com/basnijholt/agent-cli",
      "github.com/basnijholt/agent-cli.git",
    ]);
    // The service being edited is not its own broader service.
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("keeps the operator-only opt-in when editing a service that has it", async () => {
    await openEdit("OpenAI");
    openMoreOptions();
    fill("Display name", "OpenAI direct");
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "openai"],
      {
        display_name: "OpenAI direct",
        rules: [{ host: "api.openai.com", auth: { type: "bearer" } }],
        oauth_on_shared_workers: true,
      },
    );
  });

  it("words the help for refusing other paths for the operator", async () => {
    await openAdd();
    fill("Name", "limited");
    fill("Preset", "github");
    fill("GitHub repositories", "o/r");
    fireEvent.click(
      screen.getByRole("button", { name: "Use these repositories" }),
    );

    // The sentence about the administrator's services is for a user's own editor.
    const help = screen
      .getByLabelText("Refuse other paths on these hosts")
      .closest("div") as HTMLElement;
    expect(help).toHaveTextContent("are refused (path_not_allowed)");
    expect(help).not.toHaveTextContent("from your administrator");
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("shows the config validation message and puts the draft back", async () => {
    saveConfig.mockResolvedValue({
      status: "error",
      message: "Configuration validation failed",
      diagnostics: [
        {
          kind: "global",
          message: "Configuration validation failed",
          blocking: false,
        },
        {
          kind: "validation",
          issue: {
            loc: ["egress_broker", "services", "myapi", "rules", 0, "host"],
            msg: "Value error, host must not contain scheme",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["agents", "x"],
            msg: "unrelated",
            type: "value_error",
          },
        },
      ],
    });
    await addMyapi();
    save();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "myapi.rules.0.host: host must not contain scheme",
    );
    expect(alert).not.toHaveTextContent("unrelated");
    expect(updateConfigValue.mock.calls).toEqual([
      [["egress_broker", "services", "myapi"], { preset: "openai" }],
    ]);
    // The draft goes back to the state before the write, dirty flags included.
    expect(restoreConfigValue.mock.calls).toEqual([
      [["egress_broker", "services", "myapi"], undefined, clean],
    ]);
    // The editor keeps the user's input for another try.
    expect(screen.getByLabelText("Name")).toHaveValue("myapi");
  });

  it("shows an issue on the egress_broker section itself without a field path", async () => {
    saveConfig.mockResolvedValue({
      status: "error",
      message: "Configuration validation failed",
      diagnostics: [
        {
          kind: "validation",
          issue: {
            loc: ["egress_broker"],
            msg: "Value error, services must not be empty",
            type: "value_error",
          },
        },
        {
          kind: "validation",
          issue: {
            loc: ["egress_broker", "services"],
            msg: "Value error, too many services",
            type: "value_error",
          },
        },
      ],
    });
    await addMyapi();
    save();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "services must not be empty; too many services",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(": ");
  });

  it("restores the previous service when saving an edit fails", async () => {
    failSave("Configuration changed elsewhere. Your draft has not been saved.");
    await openEdit("GitHub");
    openMoreOptions();
    fireEvent.click(screen.getByLabelText("Refuse other paths on these hosts"));
    save();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Configuration changed elsewhere",
    );
    expect(restoreConfigValue.mock.calls).toEqual([
      [["egress_broker", "services", "github"], githubPreset, clean],
    ]);
  });

  it("hands back the dirty state the draft had before, other roots included", async () => {
    storeState = { ...storeState, isDirty: true, dirtyRoots: ["models"] };
    failSave();
    await addMyapi();
    save();
    fireEvent.click(
      await screen.findByRole("button", { name: "Save all changes" }),
    );

    await screen.findByText("Failed to save configuration (Error 500)");
    expect(restoreConfigValue.mock.calls).toEqual([
      [
        ["egress_broker", "services", "myapi"],
        undefined,
        { isDirty: true, dirtyRoots: ["models"], draftVersion: 0 },
      ],
    ]);
  });

  it("reports a superseded save without touching the draft again", async () => {
    saveConfig.mockResolvedValue({ status: "stale" });
    await addMyapi();
    save();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The configuration changed while saving",
    );
    expect(updateConfigValue).toHaveBeenCalledTimes(1);
    expect(restoreConfigValue).not.toHaveBeenCalled();
  });

  it("asks before a save that also writes other unsaved settings changes", async () => {
    storeState = { ...storeState, isDirty: true, dirtyRoots: ["agents"] };
    await addMyapi();
    save();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "This also saves your other unsaved settings changes.",
    );
    expect(updateConfigValue).not.toHaveBeenCalled();
    expect(saveConfig).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Save all changes" }));
    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "myapi"],
      {
        preset: "openai",
      },
    );
  });

  it("does not ask when only the egress_broker section is dirty", async () => {
    storeState = {
      ...storeState,
      isDirty: true,
      dirtyRoots: ["egress_broker"],
    };
    await addMyapi();
    save();

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/other unsaved settings changes/)).toBeNull();
  });

  it("repeats the warning when deleting, and saves nothing until confirmed", async () => {
    storeState = { ...storeState, isDirty: true, dirtyRoots: ["models"] };
    render(<EgressBroker />);
    fireEvent.click(await rowAction("Delete service for GitHub"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      "This also saves your other unsaved settings changes.",
    );
    expect(saveConfig).not.toHaveBeenCalled();
  });

  it("deletes a service from the config after confirmation", async () => {
    render(<EgressBroker />);
    fireEvent.click(await rowAction("Delete service for GitHub"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      "removes the service from the configuration",
    );
    expect(dialog).not.toHaveTextContent("other unsaved settings");
    expect(saveConfig).not.toHaveBeenCalled();
    services = [configRow("openai", "OpenAI")];
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(saveConfig).toHaveBeenCalledTimes(1));
    expect(updateConfigValue).toHaveBeenCalledWith(
      ["egress_broker", "services", "github"],
      undefined,
    );
    await waitFor(() => expect(screen.queryByLabelText("GitHub")).toBeNull());
  });

  it("shows why a delete failed and restores the service in the draft", async () => {
    failSave();
    render(<EgressBroker />);
    fireEvent.click(await rowAction("Delete service for GitHub"));
    await pressInDialog("Delete");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Failed to save configuration (Error 500)",
    );
    expect(restoreConfigValue.mock.calls).toEqual([
      [["egress_broker", "services", "github"], githubPreset, clean],
    ]);
    expect(screen.getByLabelText("GitHub")).toBeInTheDocument();
  });

  it("keeps Edit and Delete off while a save is running", async () => {
    let finish: (result: unknown) => void = () => {};
    saveConfig.mockImplementation(
      () =>
        new Promise((done) => {
          finish = done;
        }),
    );
    await openEdit("GitHub");
    openMoreOptions();
    fireEvent.click(screen.getByLabelText("Refuse other paths on these hosts"));
    save();

    await waitFor(async () =>
      expect(await rowAction("Edit service for OpenAI")).toHaveAttribute(
        "aria-disabled",
        "true",
      ),
    );
    expect(await rowAction("Delete service for OpenAI")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(await rowAction("Delete service for GitHub")).toHaveAttribute(
      "aria-disabled",
      "true",
    );

    finish({ status: "saved" });
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(await rowAction("Edit service for OpenAI")).not.toHaveAttribute(
      "aria-disabled",
    );
    expect(saveConfig).toHaveBeenCalledTimes(1);
  });

  it("asks before replacing an editor that has unsaved changes", async () => {
    await openEdit("GitHub");
    openMoreOptions();
    fill("Description", "typed but not saved");

    fireEvent.click(await rowAction("Edit service for OpenAI"));
    expect(screen.getByRole("alert")).toHaveTextContent(
      "You have unsaved changes in the open editor.",
    );
    expect(
      screen.getByRole("form", { name: "Edit github" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
    expect(screen.getByLabelText("Description")).toHaveValue(
      "typed but not saved",
    );

    fireEvent.click(await rowAction("Edit service for OpenAI"));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    expect(
      await screen.findByRole("form", { name: "Edit openai" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/unsaved changes in the open editor/)).toBeNull();
  });

  it("opens another service straight away when nothing was changed", async () => {
    await openEdit("GitHub");
    fireEvent.click(await rowAction("Edit service for OpenAI"));
    expect(
      await screen.findByRole("form", { name: "Edit openai" }),
    ).toBeInTheDocument();
  });

  it("keeps Edit off for the service whose editor is open", async () => {
    await openEdit("GitHub");
    expect(await rowAction("Edit service for GitHub")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    expect(await rowAction("Edit service for OpenAI")).not.toHaveAttribute(
      "aria-disabled",
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await rowAction("Edit service for GitHub")).not.toHaveAttribute(
      "aria-disabled",
    );
  });

  it("drops the question about another service when the editor is cancelled", async () => {
    await openEdit("GitHub");
    openMoreOptions();
    fill("Description", "typed but not saved");
    fireEvent.click(await rowAction("Edit service for OpenAI"));
    expect(
      screen.getByText(/unsaved changes in the open editor/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form")).toBeNull();
    expect(screen.queryByText(/unsaved changes in the open editor/)).toBeNull();
  });

  it("reads the draft's state from the store when the write starts, not from the last render", async () => {
    failSave();
    await addMyapi();
    // Another edit lands in the store without this panel rendering again.
    storeState = {
      ...storeState,
      isDirty: true,
      dirtyRoots: ["models"],
      draftVersion: 4,
    };
    save();

    await screen.findByText("Failed to save configuration (Error 500)");
    expect(restoreConfigValue.mock.calls).toEqual([
      [
        ["egress_broker", "services", "myapi"],
        undefined,
        { isDirty: true, dirtyRoots: ["models"], draftVersion: 4 },
      ],
    ]);
  });

  it("lists a scope's own services read-only with a badge", async () => {
    services = [
      configRow("github", "GitHub"),
      {
        ...github,
        name: "mine",
        display_name: "Mine",
        source: "user",
        rules: [],
      },
    ];
    render(<EgressBroker />);
    await screen.findByLabelText("Mine");

    expect(within(row("Mine")).getByText("User service")).toBeInTheDocument();
    await expectNoAction(/Edit|Delete/, row("Mine"));
    expect(within(row("GitHub")).queryByText("User service")).toBeNull();
    // The key can still be set on it.
    expect(
      within(row("Mine")).getByRole("button", { name: "Set key for Mine" }),
    ).toBeInTheDocument();
  });

  it("cannot edit a service the loaded config does not have", async () => {
    services = [configRow("included", "Included")];
    render(<EgressBroker />);
    fireEvent.click(await rowAction("Edit service for Included"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "not in the loaded configuration",
    );
    expect(screen.queryByRole("form")).toBeNull();
  });

  it("cannot add a service before the config is loaded", async () => {
    storeState = { agents: [], config: null, updateConfigValue, saveConfig };
    render(<EgressBroker />);
    await screen.findByLabelText("GitHub");
    expect(screen.getByRole("button", { name: "Add service" })).toBeDisabled();
  });

  it("offers Add service when no service is configured yet", async () => {
    services = [];
    render(<EgressBroker />);
    expect(
      await screen.findByText(/No egress services are configured/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add service" })).toBeEnabled();
  });

  it("refuses a name that is already used before saving", async () => {
    await openAdd();
    fill("Name", "github");
    fill("Preset", "github");
    save();
    expect(screen.getByLabelText("Name")).toHaveAccessibleDescription(
      /A service named github already exists/,
    );
    expect(saveConfig).not.toHaveBeenCalled();
  });
});
