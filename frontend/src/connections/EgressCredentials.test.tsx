import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { EgressCredentials } from "./EgressCredentials";
import type {
  AuthoredEgressService,
  EgressCredentialAgent,
  EgressCredentialService,
  EgressLogRecord,
  EgressOAuthStatus,
} from "./types";

const service = {
  name: "github",
  display_name: "GitHub",
  description: "GitHub API",
  is_shared: false,
  can_manage: true,
  configured: false,
  updated_at: null,
  active_source: null,
  key_configured: false,
  key_updated_at: null,
  oauth: null,
} satisfies EgressCredentialService;
const account: EgressOAuthStatus = {
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
const agents: EgressCredentialAgent[] = [
  {
    agent_name: "personal",
    agent_display_name: "Personal Mind",
    services: [service],
  },
  {
    agent_name: "shared_dev",
    agent_display_name: "Shared Dev",
    services: [{ ...service, is_shared: true, can_manage: false }],
  },
  { agent_name: "bare", agent_display_name: "Bare Agent", services: [] },
];

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input) === "/api/connections/egress") return json({ agents });
    throw new Error(`Unexpected request: ${String(input)}`);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress credentials page", () => {
  it("renders one section per agent with its services", async () => {
    render(<EgressCredentials />);
    const personal = await screen.findByRole("region", {
      name: "Personal Mind",
    });
    expect(
      within(personal).getByRole("button", { name: "Set GitHub API key" }),
    ).toBeInTheDocument();
    const shared = screen.getByRole("region", { name: "Shared Dev" });
    expect(within(shared).getByText("Not set")).toBeInTheDocument();
    // Nobody who cannot manage the agent can change anything on it, but the log is theirs.
    expect(
      within(shared)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Recent requests"]);
    expect(screen.getAllByRole("region")).toHaveLength(3);
    expect(
      within(screen.getByRole("region", { name: "Bare Agent" })).getByText(
        "No services are configured yet.",
      ),
    ).toBeInTheDocument();
  });

  it("explains when no agent is available", async () => {
    vi.mocked(fetch).mockImplementation(async () => json({ agents: [] }));
    render(<EgressCredentials />);
    expect(
      await screen.findByText(
        "No agents with shell or python access are available to you.",
      ),
    ).toBeInTheDocument();
  });

  it("shows an error when the listing cannot be loaded", async () => {
    vi.mocked(fetch).mockImplementation(async () => json({}, 500));
    render(<EgressCredentials />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not complete the request",
    );
  });

  it("reloads the listing after a key is saved", async () => {
    let configured = false;
    vi.mocked(fetch).mockImplementation(async (_input, options) => {
      if (options?.method === "PUT") {
        configured = true;
        return new Response(null, { status: 204 });
      }
      return json({
        agents: [
          {
            ...agents[0],
            services: [
              {
                ...service,
                configured,
                key_configured: configured,
                active_source: configured ? "key" : null,
              },
            ],
          },
        ],
      });
    });
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Set GitHub API key" }),
    );
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Replace GitHub API key" }),
      ).toBeInTheDocument(),
    );
  });

  it("reloads the listing after an account is connected and disconnected", async () => {
    let connected = false;
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    vi.mocked(fetch).mockImplementation(async (input, options) => {
      const path = String(input);
      if (path.endsWith("/connect"))
        return json({
          provider: "github",
          auth_url: "https://github.com/login/oauth/authorize",
          completion_origin: "https://portal.example.com",
        });
      if (path.endsWith("/disconnect")) {
        connected = false;
        return json({ status: "disconnected", provider: "github" });
      }
      expect(options?.method ?? "GET").toBe("GET");
      return json({
        agents: [
          {
            ...agents[0],
            services: [
              {
                ...service,
                configured: connected,
                active_source: connected ? "oauth" : null,
                oauth: {
                  ...account,
                  connected,
                  account_label: connected ? "octocat" : null,
                },
              },
            ],
          },
        ],
      });
    });
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub" }),
    );
    await waitFor(() =>
      expect(popup.location.href).toBe(
        "https://github.com/login/oauth/authorize",
      ),
    );
    connected = true;
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: "https://portal.example.com",
        source: popup as unknown as Window,
        data: {
          type: "mindroom:oauth-complete",
          provider: "github",
          status: "connected",
        },
      }),
    );
    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(
          ([input]) => String(input) === "/api/connections/egress",
        ),
    ).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Disconnect GitHub" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Disconnect",
      }),
    );
    expect(
      await screen.findByRole("button", { name: "Connect GitHub" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Connected as/)).toBeNull();
  });
});

const userService: EgressCredentialService = {
  ...service,
  name: "mine",
  display_name: "My API",
  description: "My own API",
  source: "user",
};
const configService: EgressCredentialService = {
  ...service,
  source: "config",
};

const SERVICES = "/api/connections/egress/agents/personal/services";

describe("egress services on the personal page", () => {
  let listing: EgressCredentialAgent[];
  let authored: Record<string, AuthoredEgressService>;
  let mutation: (method: string, path: string, body: unknown) => Response;
  let calls: { method: string; path: string; body: unknown }[];

  beforeEach(() => {
    listing = [
      {
        agent_name: "personal",
        agent_display_name: "Personal Mind",
        services: [configService, userService],
      },
    ];
    authored = { mine: { preset: "openai", restrict_to_rules: true } };
    mutation = () => new Response(null, { status: 204 });
    calls = [];
    vi.mocked(fetch).mockImplementation(async (input, options) => {
      const path = String(input);
      const method = options?.method ?? "GET";
      const body = options?.body ? JSON.parse(String(options.body)) : undefined;
      calls.push({ method, path, body });
      if (path === "/api/connections/egress") return json({ agents: listing });
      if (method === "GET" && path.startsWith(`${SERVICES}/`))
        return json(authored[decodeURIComponent(path.split("/").pop() ?? "")]);
      if (path.startsWith(SERVICES)) return mutation(method, path, body);
      throw new Error(`Unexpected request: ${method} ${path}`);
    });
  });

  const personal = () => screen.findByRole("region", { name: "Personal Mind" });
  const writes = () => calls.filter((call) => call.method !== "GET");

  it("adds a service and reloads the listing", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      within(await personal()).getByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "gh" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    listing[0].services = [
      ...listing[0].services,
      { ...userService, name: "gh", display_name: "GitHub work" },
    ];
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));

    expect(await screen.findByLabelText("GitHub work")).toBeInTheDocument();
    expect(writes()).toEqual([
      { method: "PUT", path: `${SERVICES}/gh`, body: { preset: "github" } },
    ]);
    // The editor closes once the service is saved.
    expect(screen.queryByRole("form")).toBeNull();
    expect(
      calls.filter((call) => call.path === "/api/connections/egress"),
    ).toHaveLength(2);
  });

  it("refuses a name that is already listed before asking the server", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      within(await personal()).getByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "mine" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service named mine already exists",
    );
    expect(writes()).toEqual([]);
  });

  it("closes the editor on cancel", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      within(await personal()).getByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    expect(
      screen.getByRole("form", { name: "Add service" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("form")).toBeNull();
    expect(writes()).toEqual([]);
  });

  it("edits a user service from its authored form and keeps the name", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Edit My API service" }),
    );
    const form = await screen.findByRole("form", { name: "Edit mine" });
    expect(within(form).getByLabelText("Name")).toBeDisabled();
    expect(within(form).getByLabelText("Preset")).toHaveValue("openai");
    expect(
      within(form).getByLabelText("Refuse other paths on these hosts"),
    ).toBeChecked();
    fireEvent.change(within(form).getByLabelText("Display name"), {
      target: { value: "Renamed" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(writes()).toEqual([
      {
        method: "PUT",
        path: `${SERVICES}/mine`,
        body: {
          preset: "openai",
          display_name: "Renamed",
          restrict_to_rules: true,
        },
      },
    ]);
  });

  it("deletes a user service after confirmation", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete My API service" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("deletes the service and its saved key");
    expect(writes()).toEqual([]);
    listing[0].services = [configService];
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByLabelText("My API")).toBeNull());
    expect(writes()).toEqual([
      { method: "DELETE", path: `${SERVICES}/mine`, body: undefined },
    ]);
  });

  it("deletes from inside the editor too", async () => {
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Edit My API service" }),
    );
    await screen.findByRole("form", { name: "Edit mine" });
    listing[0].services = [configService];
    fireEvent.click(screen.getByRole("button", { name: "Delete service" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(screen.queryByRole("form")).toBeNull());
    expect(writes().map((call) => call.method)).toEqual(["DELETE"]);
  });

  it("shows a failed delete on the row", async () => {
    mutation = () =>
      json({ detail: "Service is not a service of your own" }, 404);
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Delete My API service" }),
    );
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no longer available",
    );
    expect(screen.getByLabelText("My API")).toBeInTheDocument();
  });

  it("shows the administrator's services read-only", async () => {
    render(<EgressCredentials />);
    const row = await screen.findByLabelText("GitHub");
    expect(
      within(row).getByText("Added by your administrator"),
    ).toBeInTheDocument();
    expect(
      within(row).queryByRole("button", { name: /Edit|Delete/ }),
    ).toBeNull();
    // The key can still be set on it, as before.
    expect(
      within(row).getByRole("button", { name: "Set GitHub API key" }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByLabelText("My API")).queryByText(
        "Added by your administrator",
      ),
    ).toBeNull();
  });

  it("offers Add service on a shared agent only to those who can manage it", async () => {
    listing = [
      {
        agent_name: "readonly",
        agent_display_name: "Read Only",
        services: [{ ...configService, is_shared: true, can_manage: false }],
      },
      {
        agent_name: "managed",
        agent_display_name: "Managed",
        services: [{ ...userService, is_shared: true, can_manage: true }],
      },
      {
        agent_name: "empty",
        agent_display_name: "Empty",
        services: [],
      },
    ];
    render(<EgressCredentials />);
    const readonly = await screen.findByRole("region", { name: "Read Only" });
    expect(
      within(readonly).queryByRole("button", { name: /Add service/ }),
    ).toBeNull();
    const managed = screen.getByRole("region", { name: "Managed" });
    expect(
      within(managed).getByRole("button", { name: "Add service for Managed" }),
    ).toBeInTheDocument();
    expect(
      within(managed).getByRole("button", { name: "Edit My API service" }),
    ).toBeInTheDocument();
    // Without services there is no flag to read, so the page lets the user try.
    expect(
      within(screen.getByRole("region", { name: "Empty" })).getByRole(
        "button",
        {
          name: "Add service for Empty",
        },
      ),
    ).toBeInTheDocument();
  });

  it("turns off account login for a service on a shared agent", async () => {
    listing = [
      {
        agent_name: "personal",
        agent_display_name: "Personal Mind",
        services: [{ ...userService, is_shared: true, can_manage: true }],
      },
    ];
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "gh" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].body).toEqual({
      preset: "github",
      oauth_provider: null,
    });
  });

  it("warns about a config github service when limiting repositories", async () => {
    listing[0].services = [
      { ...configService, name: "github", display_name: "GitHub" },
    ];
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    expect(
      within(
        screen.getByRole("region", { name: "Limit to repositories" }),
      ).getByRole("note"),
    ).toHaveTextContent("GitHub is configured by your administrator");
  });

  it("shows a name conflict from the server", async () => {
    mutation = () =>
      json(
        {
          detail:
            "service name 'gh' is already used by a service your administrator configured",
        },
        409,
      );
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "gh" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "github" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "already used by a service your administrator configured",
    );
    // The editor stays open with the user's input.
    expect(screen.getByLabelText("Name")).toHaveValue("gh");
  });

  it("shows the size limit from the server", async () => {
    mutation = () => json({ detail: "A service can take at most 16 KiB" }, 413);
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "big" },
    });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A service can take at most 16 KiB",
    );
  });

  it("shows a validation message sent as a string", async () => {
    mutation = () =>
      json({ detail: "rules.0.host: host must not contain scheme" }, 422);
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "rules.0.host: host must not contain scheme",
    );
  });

  it("shows a validation message sent as FastAPI's list without echoing the input", async () => {
    mutation = () =>
      json(
        {
          detail: [
            {
              type: "string_type",
              loc: ["body", "rules", 0, "host"],
              msg: "Input should be a valid string",
              input: "SECRET-INPUT",
            },
            { type: "missing", loc: ["body"], msg: "Field required" },
          ],
        },
        422,
      );
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "rules.0.host: Input should be a valid string; Field required",
    );
    expect(alert).not.toHaveTextContent("SECRET-INPUT");
  });

  it("explains a refused write on a shared agent", async () => {
    mutation = () => json({ detail: "Credential management is required" }, 403);
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Add service for Personal Mind",
      }),
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Preset"), {
      target: { value: "openai" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save service" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only credential managers can change the services of a shared agent.",
    );
  });

  it("explains when a service to edit cannot be loaded", async () => {
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input) === "/api/connections/egress"
        ? json({ agents: listing })
        : json({ detail: "Service is not a service of your own" }, 404),
    );
    render(<EgressCredentials />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Edit My API service" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no longer available",
    );
    expect(screen.queryByRole("form")).toBeNull();
  });
});

describe("recent requests on the personal page", () => {
  const record = (changes: Partial<EgressLogRecord> = {}): EgressLogRecord => ({
    at: "2026-10-09T10:30:00+00:00",
    kind: "request",
    agent_name: "personal",
    method: "POST",
    host: "api.github.com",
    path: "/repos/acme/app/issues",
    service: "github",
    status: 201,
    ...changes,
  });
  let logs: () => Response;

  beforeEach(() => {
    logs = () =>
      json({
        records: [
          record(),
          record({
            at: "2026-10-09T10:00:00+00:00",
            method: "GET",
            host: "example.com",
            path: "/",
            service: null,
            status: 403,
            kind: "denied",
            code: "path_not_allowed",
          }),
          record({ method: "CONNECT", path: "", status: 200, kind: "tunnel" }),
        ],
      });
    vi.mocked(fetch).mockImplementation(async (input) => {
      const path = String(input);
      if (path === "/api/connections/egress") return json({ agents });
      if (path.startsWith("/api/connections/egress/logs")) return logs();
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  const logRequests = () =>
    vi
      .mocked(fetch)
      .mock.calls.map(([input]) => String(input))
      .filter((path) => path.startsWith("/api/connections/egress/logs"));
  const toggle = async (agentName = "Personal Mind") =>
    within(await screen.findByRole("region", { name: agentName })).getByRole(
      "button",
      { name: "Recent requests" },
    );

  it("starts collapsed and does not load anything until it is opened", async () => {
    render(<EgressCredentials />);
    expect(await toggle()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("table")).toBeNull();
    expect(logRequests()).toEqual([]);
  });

  it("loads the caller's 50 latest requests for that agent", async () => {
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    const table = await screen.findByRole("table", {
      name: "Recent requests for personal",
    });
    expect(logRequests()).toEqual([
      "/api/connections/egress/logs?agent_name=personal&limit=50",
    ]);
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((header) => header.textContent),
    ).toEqual(["Time", "Method", "Host", "Path", "Service", "Status"]);
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(4);
    const first = within(rows[1]).getAllByRole("cell");
    expect(first[0]).toHaveTextContent(
      new Date("2026-10-09T10:30:00+00:00").toLocaleString(),
    );
    expect(first.slice(1).map((cell) => cell.textContent)).toEqual([
      "POST",
      "api.github.com",
      "/repos/acme/app/issues",
      "github",
      "201",
    ]);
  });

  it("shows a refusal code, and dashes for a missing service or path", async () => {
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    const rows = within(await screen.findByRole("table")).getAllByRole("row");
    const refused = within(rows[2]).getAllByRole("cell");
    expect(refused[4]).toHaveTextContent("-");
    expect(refused[5]).toHaveTextContent("403");
    expect(refused[5]).toHaveTextContent("path_not_allowed");
    const tunnel = within(rows[3]).getAllByRole("cell");
    expect(tunnel[3]).toHaveTextContent("-");
    expect(tunnel[5]).not.toHaveTextContent("refused");
  });

  it("calls a denied request refused when the API sends no code", async () => {
    logs = () => json({ records: [record({ kind: "denied", status: 403 })] });
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    expect(
      await screen.findByText("refused", { selector: "span" }),
    ).toBeInTheDocument();
  });

  it("collapses and opens again, loading fresh rows each time", async () => {
    render(<EgressCredentials />);
    const button = await toggle();
    fireEvent.click(button);
    await screen.findByRole("table");
    expect(button).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(button);
    expect(screen.queryByRole("table")).toBeNull();
    expect(button).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(button);
    await screen.findByRole("table");
    expect(logRequests()).toHaveLength(2);
  });

  it("refreshes on request", async () => {
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    await screen.findByRole("table");
    logs = () => json({ records: [] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(
      await screen.findByText("No requests logged yet."),
    ).toBeInTheDocument();
    expect(logRequests()).toHaveLength(2);
  });

  it("keeps each agent's log separate", async () => {
    render(<EgressCredentials />);
    fireEvent.click(await toggle("Shared Dev"));
    await screen.findByRole("table");
    expect(logRequests()).toEqual([
      "/api/connections/egress/logs?agent_name=shared_dev&limit=50",
    ]);
    expect(
      within(screen.getByRole("region", { name: "Personal Mind" })).queryByRole(
        "table",
      ),
    ).toBeNull();
  });

  it("shows why the log is unavailable", async () => {
    logs = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Egress broker is not running",
    );
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("falls back to a general message for other failures", async () => {
    logs = () => json({ detail: "Egress logs are unavailable" }, 503);
    render(<EgressCredentials />);
    fireEvent.click(await toggle());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not complete the request. Try again.",
    );
  });
});
