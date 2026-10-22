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
import {
  credentialService,
  expectNoAction,
  oauthStatus,
  pressInDialog,
  rowAction,
} from "@/test/egressServiceTestKit";
import { Connections } from "./Connections";
import type { ConnectionService, EgressCredentialService } from "./types";

const service: ConnectionService = {
  icon: null,
  provider: "mail",
  is_shared: false,
  can_manage: true,
  connected: false,
  display_name: "Mail",
  description: "Read your email.",
  tools: ["mail"],
};
const status = {
  provider: "mail",
  connected: false,
  can_connect: true,
  reset_required: false,
  account_label: null,
  service_account: false,
};
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });

const catalog = (services: (typeof service)[]) => ({
  agents: [
    {
      agent_name: "personal",
      agent_display_name: "Personal assistant",
      is_shared: false,
      managers: [],
      can_use: true,
      mcp_available: true,
      services,
      tools: services.flatMap((item) =>
        item.tools.map((name) => ({
          name,
          display_name: item.display_name,
          description: item.description,
          provider: item.provider,
          mcp_available: true,
          icon: item.icon,
        })),
      ),
    },
  ],
});

it("falls back cleanly when an agent's Matrix picture cannot load", async () => {
  installApi({
    "/api/connections": async () => json(catalog([])),
    "/api/connections/mcp/selection": async () => json({ enabled: false }),
  });
  render(<Connections />);
  const rowButton = await screen.findByRole("button", {
    name: "Expand Personal assistant",
  });
  const image = rowButton.querySelector("img")!;
  expect(image).toHaveAttribute(
    "src",
    "/api/connections/agents/personal/avatar",
  );
  fireEvent.error(image);
  expect(rowButton.querySelector("img")).toBeNull();
  fireEvent.click(rowButton);
  expect(rowButton).toHaveAttribute("aria-expanded", "true");
});

it("shows shared connection availability without account controls for agent users", async () => {
  installApi({
    "/api/connections": async () =>
      json(catalog([{ ...service, is_shared: true, can_manage: false }])),
    "/api/connections/mcp/selection": async () =>
      json({ enabled: true, agents: {} }),
    "/api/connections/agents/personal/mail/status": async () =>
      json({ ...status, connected: true, can_connect: false }),
  });
  render(<Connections />);
  await expandAgent();
  expect(
    await screen.findByText(
      "Connected · shared with everyone using Personal assistant",
    ),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Disconnect for Mail" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Connect Mail" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("checkbox", {
      name: "Expose Mail for Personal assistant through MCP",
    }),
  ).toBeEnabled();
});

it("keeps account management available without offering MCP access to management-only agents", async () => {
  const data = catalog([service]);
  data.agents[0].can_use = false;
  installApi({
    "/api/connections": async () => json(data),
    "/api/connections/mcp/selection": async () =>
      json({ enabled: true, agents: {} }),
  });
  render(<Connections />);
  await expandAgent();
  expect(
    await screen.findByRole("button", { name: "Connect Mail" }),
  ).toBeEnabled();
  expect(
    screen.queryAllByRole("checkbox", { name: /Expose .* through MCP/ }),
  ).toHaveLength(0);
  expect(screen.getByText("Credential management only")).toBeInTheDocument();
});

describe("brokered API keys on agent cards", () => {
  const egressService = credentialService();

  it("lists the agent's API keys and refreshes the catalog after a save", async () => {
    let configured = false;
    let catalogRequests = 0;
    installApi({
      "/api/connections": async () => {
        catalogRequests += 1;
        return json(
          withEgress([
            {
              ...egressService,
              configured,
              key_configured: configured,
              active_source: configured ? "key" : null,
            },
          ]),
        );
      },
      "/api/connections/egress/agents/personal/github": async () => {
        configured = true;
        return new Response(null, { status: 204 });
      },
    });
    render(<Connections />);
    await expandAgent();
    expect(await screen.findByText("Not set")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Set key for GitHub" }));
    fireEvent.change(screen.getByLabelText("GitHub API key"), {
      target: { value: "abc" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await rowAction("Replace key for GitHub")).toBeInTheDocument();
    expect(catalogRequests).toBe(2);
    expect(
      screen.getByRole("button", { name: "Collapse Personal assistant" }),
    ).toBeInTheDocument();
  });

  it("shows no API key section for agents without egress services", async () => {
    render(<Connections />);
    await expandAgent();
    await screen.findByRole("button", { name: "Connect Mail" });
    expect(
      screen.queryByRole("heading", { name: "API keys and accounts" }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: "Manage services" })).toBeNull();
  });

  it("shows no API key section when the catalog sends null for the agent", async () => {
    installEgress(null);
    render(<Connections />);
    await expandAgent();
    await screen.findByRole("button", { name: "Connect Mail" });
    expect(
      screen.queryByRole("heading", { name: "API keys and accounts" }),
    ).toBeNull();
    expect(screen.queryByRole("link", { name: "Manage services" })).toBeNull();
  });

  it("links an eligible agent without services to the page where one can be added", async () => {
    installEgress([]);
    render(<Connections />);
    await expandAgent();

    expect(
      await screen.findByRole("heading", { name: "API keys and accounts" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Manage services" }),
    ).toHaveAttribute(
      "href",
      `/connections/egress#egress-agent-${catalog([service]).agents[0].agent_name}`,
    );
    expect(
      screen.getByText("No services are set up for this agent yet."),
    ).toBeInTheDocument();
    // There is nothing to set a key on yet.
    await expectNoAction(/^(Set|Replace|Remove) key for /);
  });

  it("links the API key section to the agent's services on the personal page", async () => {
    installEgress([egressService]);
    render(<Connections />);
    await expandAgent();

    const link = await screen.findByRole("link", { name: "Manage services" });
    expect(link).toHaveAttribute(
      "href",
      `/connections/egress#egress-agent-${catalog([service]).agents[0].agent_name}`,
    );
  });

  it("says View services instead of Manage services to someone who can change none of them", async () => {
    installEgress([{ ...egressService, is_shared: true, can_manage: false }]);
    render(<Connections />);
    await expandAgent();

    const link = await screen.findByRole("link", { name: "View services" });
    expect(link).toHaveAttribute(
      "href",
      `/connections/egress#egress-agent-${catalog([service]).agents[0].agent_name}`,
    );
    expect(screen.queryByRole("link", { name: "Manage services" })).toBeNull();
  });

  it("keeps Manage services when the viewer can change at least one service", async () => {
    installEgress([
      { ...egressService, can_manage: false },
      { ...egressService, name: "openai", display_name: "OpenAI" },
    ]);
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByRole("link", { name: "Manage services" }),
    ).toBeVisible();
    expect(screen.queryByRole("link", { name: "View services" })).toBeNull();
  });

  it("shows a limit on the agent's card as a row that only limits, and the administrator's row as limited", async () => {
    const rule = (host: string, path_prefix: string) => ({
      host,
      port: null,
      path_prefix,
    });
    installEgress([
      {
        ...egressService,
        source: "config",
        rules: [rule("api.github.com", "/"), rule("github.com", "/")],
      },
      {
        ...egressService,
        name: "github-limited",
        display_name: "GitHub (octocat/Hello-World)",
        source: "user",
        restrict_only: true,
        rules: [
          rule("api.github.com", "/repos/octocat/Hello-World"),
          rule("github.com", "/octocat/Hello-World"),
          rule("github.com", "/octocat/Hello-World.git"),
        ],
      },
    ]);
    render(<Connections />);
    await expandAgent();

    const github = await screen.findByLabelText("GitHub");
    expect(
      within(github).getByText("Limited to octocat/Hello-World"),
    ).toBeVisible();
    // The portal has no editor, so there is nothing to change or remove here.
    await expectNoAction(/limit/i);
    const limit = screen.getByLabelText("GitHub (octocat/Hello-World)");
    expect(within(limit).getByText("Limit only")).toBeVisible();
    expect(within(limit).queryByRole("button")).toBeNull();
  });

  it("tells a personal agent's user that its sandbox is shared, not that the agent is", async () => {
    installEgress([
      {
        ...egressService,
        oauth: oauthStatus({
          can_connect: false,
          unavailable_reason: "shared_sandbox",
        }),
      },
    ]);
    render(<Connections />);
    await expandAgent();

    expect(
      await screen.findByText(
        "Personal GitHub accounts can't be used in this sandbox, which other users' commands share. Add an API key instead.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/shared agents/)).toBeNull();
  });

  describe("one account in both sections", () => {
    // The tool section and the API keys section show the same Mail account.
    let connected: boolean;
    let catalogRequests: number;
    beforeEach(() => {
      connected = true;
      catalogRequests = 0;
      const disconnect = async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      };
      installApi({
        "/api/connections": async () => {
          catalogRequests += 1;
          return json(
            withEgress([
              {
                ...egressService,
                name: "gmail",
                display_name: "Gmail",
                configured: connected,
                active_source: connected ? "oauth" : null,
                oauth: oauthStatus({
                  provider: "mail",
                  display_name: "Mail",
                  connected,
                  account_label: connected ? "user@example.com" : null,
                }),
              },
            ]),
          );
        },
        "/api/connections/agents/personal/mail/status": async () =>
          json({
            ...status,
            connected,
            account_label: connected ? "user@example.com" : null,
          }),
        "/api/connections/agents/personal/mail/disconnect": disconnect,
        "/api/connections/egress/agents/personal/gmail/disconnect": disconnect,
      });
    });
    const gmailRow = () => screen.getByRole("row", { name: "Gmail" });

    it("refreshes the API keys section after a disconnect in the tool section", async () => {
      render(<Connections />);
      await expandAgent();
      fireEvent.click(
        await screen.findByRole("button", { name: "Disconnect for Mail" }),
      );
      await pressInDialog("Disconnect");

      expect(await within(gmailRow()).findByText("Not set")).toBeVisible();
      expect(catalogRequests).toBe(2);
    });

    it("refreshes the tool section after a disconnect in the API keys section", async () => {
      render(<Connections />);
      await expandAgent();
      await screen.findByRole("button", { name: "Disconnect for Mail" });
      fireEvent.click(await rowAction("Disconnect for Gmail", gmailRow()));
      await pressInDialog("Disconnect");

      expect(
        await screen.findByRole("button", { name: "Connect Mail" }),
      ).toBeEnabled();
      expect(within(gmailRow()).getByText("Not set")).toBeVisible();
    });
  });

  it("connects an account from the agent card and refreshes the catalog", async () => {
    let connected = false;
    let catalogRequests = 0;
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
      document: document.implementation.createHTMLDocument(),
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    installApi({
      "/api/connections": async () => {
        catalogRequests += 1;
        return json(
          withEgress([
            {
              ...egressService,
              configured: connected,
              active_source: connected ? "oauth" : null,
              oauth: oauthStatus({
                connected,
                account_label: connected ? "octocat" : null,
              }),
            },
          ]),
        );
      },
      "/api/connections/egress/agents/personal/github/connect": async () =>
        json({
          provider: "github",
          auth_url: "https://github.com/login/oauth/authorize",
          completion_origin: "https://portal.example.com",
        }),
    });
    render(<Connections />);
    await expandAgent();
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect GitHub for GitHub" }),
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
          type: "mindroom-oauth",
          provider: "github",
          status: "connected",
        },
      }),
    );
    expect(await screen.findByText("octocat")).toBeInTheDocument();
    expect(catalogRequests).toBe(2);
    expect(await rowAction("Disconnect for GitHub")).toBeInTheDocument();
  });
});

it("keeps agents that are not available through MCP out of the MCP selection", async () => {
  const data = catalog([{ ...service, is_shared: true, can_manage: false }]);
  data.agents[0].mcp_available = false;
  installApi({
    "/api/connections": async () => json(data),
    "/api/connections/mcp/selection": async () =>
      json({ enabled: true, agents: {} }),
    "/api/connections/agents/personal/mail/status": async () =>
      json({ ...status, connected: true, can_connect: false }),
  });
  render(<Connections />);
  await expandAgent();
  expect(
    await screen.findByText(/shared with everyone using Personal assistant/),
  ).toBeInTheDocument();
  expect(
    screen.queryAllByRole("checkbox", { name: /Expose .* through MCP/ }),
  ).toHaveLength(0);
  expect(screen.getAllByText("MindRoom only")).toHaveLength(2);
});

it("does not explain MCP availability while the gateway is disabled", async () => {
  const data = catalog([service]);
  data.agents[0].mcp_available = false;
  installApi({
    "/api/connections": async () => json(data),
    "/api/connections/mcp/selection": async () => json({ enabled: false }),
  });
  render(<Connections />);
  await screen.findByRole("button", { name: "Expand Personal assistant" });
  expect(
    screen.queryByTitle(
      "Runs tools with shared credentials, so it is not exposed through MCP",
    ),
  ).not.toBeInTheDocument();
});

/** The catalog of the personal agent, with these API key services. */
const withEgress = (egress_services: unknown[] | null) => ({
  agents: [{ ...catalog([service]).agents[0], egress_services }],
});
const installEgress = (egress_services: unknown[] | null) =>
  installApi({
    "/api/connections": async () => json(withEgress(egress_services)),
  });

function installApi(overrides: Record<string, () => Promise<Response>> = {}) {
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const path = String(input);
    if (overrides[path]) return overrides[path]();
    if (path === "/api/connections") return json(catalog([service]));
    if (path === "/api/connections/mcp/selection")
      return json({ enabled: false, agents: {} });
    if (path === "/api/connections/mcp/clients")
      return json({ enabled: false, clients: [] });
    if (path === "/api/connections/me")
      return json({
        matrix_user_id: "@bob:example.org",
        display_name: "Bob Builder",
        email: null,
        is_administrator: false,
        egress_enabled: true,
      });
    if (path === "/api/connections/agents/personal/mail/status")
      return json(status);
    if (path.endsWith("/disconnect")) {
      expect(options).toMatchObject({ method: "POST", body: "{}" });
      return json({ status: "disconnected", provider: "mail" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

async function expandAgent(name = "Personal assistant") {
  fireEvent.click(
    await screen.findByRole("button", { name: `Expand ${name}` }),
  );
}

beforeEach(() => installApi());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("connections", () => {
  it("keeps service connections usable when MCP access is not provisioned", async () => {
    installApi({
      "/api/connections/mcp/selection": async () =>
        json({
          enabled: false,
          agents: {},
          unavailable_reason: "account_required",
        }),
    });
    render(<Connections />);
    expect(
      await screen.findByText(/MCP access isn't enabled for your account/),
    ).toBeInTheDocument();
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("table", { name: "Personal assistant tools" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reload selection" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Connected MCP clients" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryAllByRole("checkbox", { name: /Expose .* through MCP/ }),
    ).toHaveLength(0);
  });

  it("keeps service connections usable alongside a retryable MCP failure", async () => {
    installApi({
      "/api/connections/mcp/selection": async () =>
        json({ detail: "Request timed out" }, 408),
    });
    render(<Connections />);
    expect(
      await screen.findByText(/Could not load MCP selection/),
    ).toBeInTheDocument();
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Reload selection" }),
    ).toBeEnabled();
    expect(
      screen.queryByText(/MCP access isn't enabled for your account/),
    ).not.toBeInTheDocument();
  });

  it("keeps agents collapsed and loads connections only after expansion", async () => {
    render(<Connections />);
    expect(
      await screen.findByRole("table", { name: "Agents" }),
    ).toBeInTheDocument();
    const expand = screen.getByRole("button", {
      name: "Expand Personal assistant",
    });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/connections/agents/personal/mail/status",
      expect.anything(),
    );
    fireEvent.click(expand);
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("table", { name: "Personal assistant tools" }),
    ).toBeInTheDocument();
  });

  it("shows only the server's services without loading dashboard configuration", async () => {
    render(<Connections />);
    await expandAgent();
    expect(await screen.findByText("Mail")).toBeInTheDocument();
    expect(screen.getByText("Personal assistant")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("combobox", { name: "Filter agents" }),
    ).toBeInTheDocument();
    expect(
      vi
        .mocked(fetch)
        .mock.calls.map(([url]) => String(url))
        .sort(),
    ).toEqual(
      [
        "/api/connections",
        "/api/connections/agents/personal/mail/status",
        "/api/connections/mcp/clients",
        "/api/connections/mcp/selection",
        "/api/connections/me",
      ].sort(),
    );
  });

  it("loads statuses concurrently and isolates a failed provider", async () => {
    let finishMail!: (response: Response) => void;
    installApi({
      "/api/connections": async () =>
        json(
          catalog([
            service,
            {
              ...service,
              provider: "calendar",
              display_name: "Calendar",
              tools: ["calendar"],
            },
          ]),
        ),
      "/api/connections/agents/personal/mail/status": () =>
        new Promise((resolve) => {
          finishMail = resolve;
        }),
      "/api/connections/agents/personal/calendar/status": async () =>
        json({
          ...status,
          provider: "calendar",
          connected: true,
          account_label: "user@example.com",
        }),
    });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Disconnect for Calendar" }),
    ).toBeEnabled();
    await act(async () =>
      finishMail(json({ detail: "Internal configuration details" }, 500)),
    );
    expect(
      await screen.findByText(/could not load.*status/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Internal configuration details"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Disconnect for Calendar" }),
    ).toBeEnabled();
  });

  it("keeps connection rows usable while the client list fails", async () => {
    installApi({
      "/api/connections/mcp/clients": async () =>
        json({ detail: "Sensitive server detail" }, 500),
    });

    render(<Connections />);
    await expandAgent();

    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load connected clients",
    );
    expect(
      screen.queryByText("Sensitive server detail"),
    ).not.toBeInTheDocument();
  });

  it("requires confirmation before disconnecting, then refreshes status", async () => {
    let connected = true;
    installApi({
      "/api/connections/agents/personal/mail/status": async () =>
        json({
          ...status,
          connected,
          account_label: connected ? "user@example.com" : null,
        }),
      "/api/connections/agents/personal/mail/disconnect": async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect for Mail" }),
    );
    expect(connected).toBe(true);
    const dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(connected).toBe(false);
  });

  it("offers reset confirmation for unreadable credentials before reconnecting", async () => {
    let resetRequired = true;
    installApi({
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, reset_required: resetRequired }),
      "/api/connections/agents/personal/mail/disconnect": async () => {
        resetRequired = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    fireEvent.click(
      await screen.findByRole("button", { name: "Reset connection for Mail" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Reset connection",
      }),
    );
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
  });

  it.each([undefined, "account_required"])(
    "completes service authorization with MCP availability %s",
    async (unavailableReason) => {
      const popup = {
        closed: false,
        close: vi.fn(),
        location: { href: "about:blank" },
        document: document.implementation.createHTMLDocument(),
      };
      const open = vi
        .spyOn(window, "open")
        .mockReturnValue(popup as unknown as Window);
      let connected = false;
      installApi({
        "/api/connections/mcp/selection": async () =>
          json({
            enabled: false,
            agents: {},
            unavailable_reason: unavailableReason,
          }),
        "/api/connections/agents/personal/mail/status": async () =>
          json({ ...status, connected }),
        "/api/connections/agents/personal/mail/connect": async () => {
          expect(open).toHaveBeenCalledOnce();
          return json({
            provider: "mail",
            auth_url: "https://auth.example.com/start",
            completion_origin: window.location.origin,
          });
        },
      });
      render(<Connections />);
      await expandAgent();
      fireEvent.click(
        await screen.findByRole("button", { name: "Connect Mail" }),
      );
      expect(open).toHaveBeenCalledOnce();
      await waitFor(() =>
        expect(popup.location.href).toBe("https://auth.example.com/start"),
      );
      connected = true;
      act(() =>
        window.dispatchEvent(
          new MessageEvent("message", {
            origin: window.location.origin,
            source: popup as unknown as Window,
            data: {
              type: "mindroom-oauth",
              provider: "mail",
              status: "connected",
            },
          }),
        ),
      );
      expect(
        await screen.findByRole("button", { name: "Disconnect for Mail" }),
      ).toBeEnabled();
    },
  );

  it.each([
    [true, "Disconnect for Mail"],
    [false, "Connect Mail"],
  ])(
    "reads the status once when the popup closes without answering, and treats connected=%s as the answer",
    async (connectedAfter, expectedButton) => {
      const popup = {
        closed: false,
        close: vi.fn(),
        location: { href: "about:blank" },
        document: document.implementation.createHTMLDocument(),
      };
      vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
      let connected = false;
      let statusReads = 0;
      installApi({
        "/api/connections/agents/personal/mail/status": async () => {
          statusReads += 1;
          return json({ ...status, connected });
        },
        "/api/connections/agents/personal/mail/connect": async () =>
          json({
            provider: "mail",
            auth_url: "https://auth.example.com/start",
            completion_origin: window.location.origin,
          }),
      });
      render(<Connections />);
      await expandAgent();
      fireEvent.click(
        await screen.findByRole("button", { name: "Connect Mail" }),
      );
      await waitFor(() =>
        expect(popup.location.href).toBe("https://auth.example.com/start"),
      );
      const readsBefore = statusReads;
      connected = connectedAfter;
      popup.closed = true;
      if (!connectedAfter)
        expect(
          await screen.findByRole("alert", {}, { timeout: 3000 }),
        ).toHaveTextContent("Authorization was cancelled.");
      expect(
        await screen.findByRole(
          "button",
          { name: expectedButton },
          { timeout: 3000 },
        ),
      ).toBeEnabled();
      // One read to decide; a connection also reloads the portal's own status.
      if (connectedAfter)
        expect(statusReads - readsBefore).toBeGreaterThanOrEqual(1);
      else expect(statusReads - readsBefore).toBe(1);
    },
  );

  it("points an agent's toggle at its details only while they are shown", async () => {
    installApi();
    render(<Connections />);
    const toggle = await screen.findByRole("button", {
      name: "Expand Personal assistant",
    });
    expect(toggle).not.toHaveAttribute("aria-controls");
    fireEvent.click(toggle);
    const opened = screen.getByRole("button", {
      name: "Collapse Personal assistant",
    });
    expect(
      document.getElementById(opened.getAttribute("aria-controls") ?? ""),
    ).not.toBeNull();
    fireEvent.click(opened);
    expect(
      screen.getByRole("button", { name: "Expand Personal assistant" }),
    ).not.toHaveAttribute("aria-controls");
  });

  it("shows an empty state for no configured services", async () => {
    installApi({
      "/api/connections": async () => json(catalog([])),
    });
    render(<Connections />);
    await expandAgent();
    expect(await screen.findByText(/no tools.*available/i)).toBeInTheDocument();
  });

  it("shows safe access errors without displaying backend details", async () => {
    installApi({
      "/api/connections": async () =>
        json({ detail: "Private internal details" }, 403),
    });
    render(<Connections />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /not available.*account/i,
    );
    expect(
      screen.queryByText("Private internal details"),
    ).not.toBeInTheDocument();
  });

  it("shows a safe error when an expired session returns an HTML page", async () => {
    installApi({
      "/api/connections": async () =>
        new Response("<html>Login required</html>"),
    });
    render(<Connections />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not read.*response/i,
    );
    expect(screen.queryByText(/<html>/)).not.toBeInTheDocument();
  });

  it("keeps an unavailable provider disabled and says why", async () => {
    installApi({
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, can_connect: false }),
    });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeDisabled();
    expect(
      screen.getByText(
        "This service needs its OAuth client saved in the MindRoom dashboard before it can connect.",
      ),
    ).toBeInTheDocument();
    cleanup();

    installApi({
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, can_connect: false, service_account: true }),
    });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByText(
        "This service uses a shared service account, so there is no account to connect here.",
      ),
    ).toBeInTheDocument();
  });

  it("closes an in-flight popup when the portal unmounts", async () => {
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
      document: document.implementation.createHTMLDocument(),
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    installApi({
      "/api/connections/agents/personal/mail/connect": () =>
        new Promise(() => {}),
    });
    const { unmount } = render(<Connections />);
    await expandAgent();
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Mail" }),
    );
    unmount();
    await waitFor(() => expect(popup.close).toHaveBeenCalledOnce());
  });
});

describe("shared agent connections", () => {
  it("refreshes other agents using the same account after disconnect", async () => {
    let connected = true;
    const sharedStatus = async () => json({ ...status, connected });
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            ...catalog([service]).agents,
            {
              agent_name: "research",
              agent_display_name: "Research Team",
              is_shared: true,
              managers: [],
              tools: catalog([service]).agents[0].tools,
              services: [service],
            },
          ],
        }),
      "/api/connections/agents/personal/mail/status": sharedStatus,
      "/api/connections/agents/research/mail/status": sharedStatus,
      "/api/connections/agents/research/mail/disconnect": async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    await expandAgent("Research Team");
    const personal = await screen.findByRole("region", {
      name: "Personal assistant",
    });
    const shared = await screen.findByRole("region", { name: "Research Team" });
    expect(
      await within(personal).findByRole("button", {
        name: "Disconnect for Mail",
      }),
    ).toBeEnabled();
    fireEvent.click(
      await within(shared).findByRole("button", {
        name: "Disconnect for Mail",
      }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Disconnect",
      }),
    );
    expect(
      await within(shared).findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      await within(personal).findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
  });

  it("loads MCP client controls for a shared-only manager", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            {
              agent_name: "research",
              agent_display_name: "Research Team",
              is_shared: true,
              managers: [],
              tools: catalog([service]).agents[0].tools,
              services: [service],
            },
          ],
        }),
      "/api/connections/agents/research/mail/status": async () => json(status),
    });
    render(<Connections />);
    await expandAgent("Research Team");
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(vi.mocked(fetch).mock.calls.map(([url]) => String(url))).toContain(
      "/api/connections/mcp/clients",
    );
  });

  it("keeps the same provider separate per agent and identifies shared disconnects", async () => {
    let sharedConnected = true;
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            ...catalog([service]).agents,
            {
              agent_name: "research",
              agent_display_name: "Research Team",
              is_shared: true,
              managers: [],
              tools: catalog([service]).agents[0].tools,
              services: [{ ...service, is_shared: true }],
            },
          ],
        }),
      "/api/connections/agents/research/mail/status": async () =>
        json({
          ...status,
          connected: sharedConnected,
          account_label: sharedConnected ? "team@example.org" : null,
        }),
      "/api/connections/agents/research/mail/disconnect": async () => {
        sharedConnected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    await expandAgent("Research Team");
    const personal = await screen.findByRole("region", {
      name: "Personal assistant",
    });
    const shared = await screen.findByRole("region", { name: "Research Team" });
    expect(
      await within(personal).findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(
      screen
        .getByRole("button", { name: "Collapse Research Team" })
        .closest("tr"),
    ).toHaveTextContent("Shared");
    fireEvent.click(
      await within(shared).findByRole("button", {
        name: "Disconnect for Mail",
      }),
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(
      "This removes the saved Mail connection for Research Team.",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Disconnect" }));
    expect(
      await within(shared).findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(sharedConnected).toBe(false);
    expect(
      within(personal).getByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
  });
});

it("does not call a requester-only connection on a shared agent shared", async () => {
  installApi({
    "/api/connections": async () =>
      json({
        agents: [
          {
            agent_name: "research",
            agent_display_name: "Research Team",
            is_shared: true,
            managers: [],
            tools: catalog([service]).agents[0].tools,
            services: [service],
          },
        ],
      }),
    "/api/connections/agents/research/mail/status": async () =>
      json({ ...status, connected: true }),
  });
  render(<Connections />);
  await expandAgent("Research Team");
  fireEvent.click(
    await screen.findByRole("button", { name: "Disconnect for Mail" }),
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent(
    "This removes the saved Mail connection for Research Team.",
  );
  expect(screen.queryByText(/shared with everyone/i)).toBeNull();
});

describe("MCP agent selection", () => {
  const selectionPath = "/api/connections/mcp/selection";
  const withTools = () => ({
    agents: [
      {
        ...catalog([service]).agents[0],
        tools: [
          ...catalog([service]).agents[0].tools,
          {
            name: "calculator",
            display_name: "Calculator",
            description: "Calculate numbers.",
            provider: null,
            icon: null,
            mcp_available: true,
          },
          {
            name: "matrix_message",
            display_name: "Matrix Message",
            description: "Send room messages.",
            provider: null,
            icon: null,
            mcp_available: false,
          },
        ],
      },
      {
        agent_name: "shared",
        agent_display_name: "Research Team",
        is_shared: true,
        managers: [],
        can_use: true,
        mcp_available: true,
        services: [],
        tools: [
          {
            name: "web",
            display_name: "Web Search",
            description: "Search the web.",
            provider: null,
            icon: null,
            mcp_available: true,
          },
        ],
      },
    ],
  });

  it("lists tools without authentication and marks room-dependent tools", async () => {
    installApi({ "/api/connections": async () => json(withTools()) });
    render(<Connections />);
    await expandAgent();
    expect(screen.queryByText("Calculator")).not.toBeInTheDocument();
    const other = screen.getByRole("button", {
      name: "Other tools for Personal assistant",
    });
    expect(other).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(other);
    expect(await screen.findByText("Calculator")).toBeInTheDocument();
    expect(screen.getByText("Matrix Message")).toBeInTheDocument();
    expect(screen.getByText("MindRoom only")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Connect Calculator" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("keeps individual tool choices separate per agent and supports all or off", async () => {
    let selected: Record<string, string[] | null> = { personal: null };
    const data = withTools();
    data.agents[1].tools = data.agents[0].tools.filter(
      (tool) => tool.provider === null,
    );
    installApi({ "/api/connections": async () => json(data) });
    const fallback = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, options) => {
      if (String(input) !== selectionPath) return fallback(input, options);
      if (options?.method === "POST")
        selected = JSON.parse(String(options.body)).agents;
      return json({ enabled: true, agents: selected });
    });
    render(<Connections />);
    await expandAgent();
    await expandAgent("Research Team");
    for (const name of ["Personal assistant", "Research Team"]) {
      fireEvent.click(
        screen.getByRole("button", { name: `Other tools for ${name}` }),
      );
    }
    const personalTool = screen.getByRole("checkbox", {
      name: "Expose Calculator for Personal assistant through MCP",
    });
    const sharedTool = screen.getByRole("checkbox", {
      name: "Expose Calculator for Research Team through MCP",
    });
    const personal = screen.getByRole("checkbox", {
      name: "Expose Personal assistant through MCP",
    });
    expect(personalTool).toBeChecked();
    expect(sharedTool).not.toBeChecked();
    fireEvent.click(personalTool);
    await waitFor(() => expect(selected).toEqual({ personal: ["mail"] }));
    expect(personal).toHaveAttribute("aria-checked", "mixed");
    fireEvent.click(sharedTool);
    await waitFor(() =>
      expect(selected).toEqual({ personal: ["mail"], shared: ["calculator"] }),
    );
    expect(personalTool).not.toBeChecked();
    expect(sharedTool).toBeChecked();
    fireEvent.click(sharedTool);
    await waitFor(() => expect(selected).toEqual({ personal: ["mail"] }));
    fireEvent.click(personal);
    await waitFor(() => expect(selected).toEqual({ personal: null }));
    expect(personalTool).toBeChecked();
    fireEvent.click(personal);
    await waitFor(() => expect(selected).toEqual({}));
    expect(personalTool).not.toBeChecked();
    expect(
      screen.queryByRole("checkbox", { name: /Expose Matrix Message/ }),
    ).not.toBeInTheDocument();
  });

  it("filters by tool name, agent type, and MCP access without fetching hidden statuses", async () => {
    installApi({
      "/api/connections": async () => json(withTools()),
      [selectionPath]: async () =>
        json({ enabled: true, agents: { personal: null } }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Research Team" });
    const search = screen.getByRole("searchbox", {
      name: "Search agents, tools, or services",
    });
    const filter = screen.getByRole("combobox", { name: "Filter agents" });
    fireEvent.change(search, { target: { value: "WEB SEARCH" } });
    expect(
      screen.queryByRole("button", { name: "Expand Personal assistant" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Expand Research Team" }),
    ).toBeInTheDocument();
    fireEvent.change(search, { target: { value: "" } });
    fireEvent.change(filter, { target: { value: "personal" } });
    expect(
      screen.queryByRole("button", { name: "Expand Research Team" }),
    ).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "shared" } });
    expect(
      screen.queryByRole("button", { name: "Expand Personal assistant" }),
    ).not.toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "exposed" } });
    expect(
      screen.getByRole("button", { name: "Expand Personal assistant" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Expand Research Team" }),
    ).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalledWith(
      "/api/connections/agents/personal/mail/status",
      expect.anything(),
    );
  });

  it.each(["constructor", "__proto__"])(
    "handles an agent named %s independently of object keys",
    async (name) => {
      const data = catalog([]);
      data.agents[0].agent_name = name;
      installApi({
        "/api/connections": async () => json(data),
        [selectionPath]: async () => json({ enabled: true, agents: {} }),
      });
      const fallback = vi.mocked(fetch).getMockImplementation()!;
      vi.mocked(fetch).mockImplementation(async (input, options) => {
        if (String(input) === selectionPath && options?.method === "POST") {
          const { agents } = JSON.parse(String(options.body));
          expect(Object.keys(agents)).toEqual([name]);
          expect(agents[name]).toBeNull();
          return json({ enabled: true, agents });
        }
        return fallback(input, options);
      });
      render(<Connections />);
      expect(
        await screen.findByRole("button", {
          name: "Expand Personal assistant",
        }),
      ).toHaveAttribute("aria-expanded", "false");
      const checkbox = screen.getByRole("checkbox", {
        name: "Expose Personal assistant through MCP",
      });
      expect(checkbox).not.toBeChecked();
      fireEvent.click(checkbox);
      await waitFor(() => expect(checkbox).toBeChecked());
    },
  );

  it("renders each toolkit separately while sharing one provider connection", async () => {
    const data = catalog([{ ...service, tools: ["mail", "contacts"] }]);
    data.agents[0].tools[1].display_name = "Contacts";
    installApi({ "/api/connections": async () => json(data) });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    const rows = within(
      screen.getByRole("table", { name: "Personal assistant tools" }),
    ).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("Mail");
    expect(rows[2]).toHaveTextContent("Contacts");
    expect(
      screen.getAllByRole("button", { name: "Connect Mail" }),
    ).toHaveLength(1);
    expect(
      vi
        .mocked(fetch)
        .mock.calls.filter(([url]) => String(url).endsWith("/mail/status")),
    ).toHaveLength(1);
  });

  it("saves one complete selection for all clients, including empty", async () => {
    let selected: Record<string, string[] | null> = { personal: null };
    installApi({ "/api/connections": async () => json(withTools()) });
    const fallback = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, options) => {
      if (String(input) !== selectionPath) return fallback(input, options);
      if (options?.method === "POST")
        selected = JSON.parse(String(options.body)).agents;
      return json({ enabled: true, agents: selected });
    });
    render(<Connections />);
    await expandAgent();
    const personal = await screen.findByRole("checkbox", {
      name: "Expose Personal assistant through MCP",
    });
    const shared = screen.getByRole("checkbox", {
      name: "Expose Research Team through MCP",
    });
    expect(personal).toBeChecked();
    expect(shared).not.toBeChecked();
    expect(screen.getByText(/every connected MCP client/i)).toBeInTheDocument();
    fireEvent.click(shared);
    await waitFor(() => expect(shared).toBeChecked());
    expect(selected).toEqual({ personal: null, shared: null });
    fireEvent.click(personal);
    await waitFor(() => expect(personal).not.toBeChecked());
    fireEvent.click(shared);
    await waitFor(() => expect(shared).not.toBeChecked());
    expect(selected).toEqual({});
    expect(screen.getByRole("button", { name: "Connect Mail" })).toBeEnabled();
  });

  it("serializes saves and retains saved state on failure", async () => {
    let finish!: (value: Response) => void;
    installApi({ "/api/connections": async () => json(withTools()) });
    const fallback = vi.mocked(fetch).getMockImplementation()!;
    vi.mocked(fetch).mockImplementation(async (input, options) => {
      if (String(input) !== selectionPath) return fallback(input, options);
      if (options?.method === "POST")
        return new Promise((resolve) => {
          finish = resolve;
        });
      return json({ enabled: true, agents: { personal: null } });
    });
    render(<Connections />);
    await expandAgent();
    const personal = await screen.findByRole("checkbox", {
      name: "Expose Personal assistant through MCP",
    });
    const shared = screen.getByRole("checkbox", {
      name: "Expose Research Team through MCP",
    });
    fireEvent.click(shared);
    expect(personal).toBeDisabled();
    expect(shared).toBeDisabled();
    await act(async () => finish(json({}, 500)));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not save.*selection/i,
    );
    expect(personal).toBeChecked();
    expect(shared).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Connect Mail" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Reload selection" }));
    await waitFor(() => expect(personal).toBeEnabled());
  });
});

describe("shared accounts on agent cards", () => {
  const sharedCatalog = (egress_services?: unknown[]) => ({
    agents: [
      {
        agent_name: "research",
        agent_display_name: "Research Team",
        is_shared: true,
        managers: [],
        can_use: true,
        services: [{ ...service, is_shared: true }],
        tools: catalog([service]).agents[0].tools,
        ...(egress_services ? { egress_services } : {}),
      },
    ],
  });
  const statusPath = "/api/connections/agents/research/mail/status";

  it("shows a connected shared account to its managers and to everyone else", async () => {
    for (const canManage of [true, false]) {
      installApi({
        "/api/connections": async () =>
          json({
            agents: [
              {
                ...sharedCatalog().agents[0],
                services: [
                  { ...service, is_shared: true, can_manage: canManage },
                ],
              },
            ],
          }),
        [statusPath]: async () =>
          json({
            ...status,
            connected: true,
            can_connect: canManage,
            account_label: "team@example.org",
          }),
      });
      const { unmount } = render(<Connections />);
      await expandAgent("Research Team");
      expect(
        await screen.findByText(
          "Connected as team@example.org · shared with everyone using Research Team",
        ),
      ).toBeInTheDocument();
      unmount();
    }
  });

  describe("Connect", () => {
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
      document: document.implementation.createHTMLDocument(),
    };
    beforeEach(() => {
      vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    });

    const renderShared = async (connectCalls: string[]) => {
      installApi({
        "/api/connections": async () => json(sharedCatalog()),
        [statusPath]: async () => json(status),
        "/api/connections/agents/research/mail/connect": async () => {
          connectCalls.push("connect");
          return new Promise(() => {});
        },
      });
      render(<Connections />);
      await expandAgent("Research Team");
      fireEvent.click(
        await screen.findByRole("button", { name: "Connect Mail" }),
      );
      return screen.findByRole("dialog", { name: "Connect a shared account?" });
    };

    it("asks Connect a shared account? and only goes on after Continue", async () => {
      const connectCalls: string[] = [];
      const dialog = await renderShared(connectCalls);
      expect(
        within(dialog).getByText(
          "Everyone who uses Research Team will act as the Mail account you pick. Use a team account, not your personal one.",
        ),
      ).toBeVisible();
      expect(connectCalls).toEqual([]);

      fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
      await waitFor(() => expect(connectCalls).toEqual(["connect"]));
    });

    it("sends nothing when the question is cancelled", async () => {
      const connectCalls: string[] = [];
      const dialog = await renderShared(connectCalls);
      fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(connectCalls).toEqual([]);
      expect(window.open).not.toHaveBeenCalled();
    });

    it("does not ask for an agent's own account", async () => {
      const connectCalls: string[] = [];
      installApi({
        "/api/connections/agents/personal/mail/connect": async () => {
          connectCalls.push("connect");
          return new Promise(() => {});
        },
      });
      render(<Connections />);
      await expandAgent();
      fireEvent.click(
        await screen.findByRole("button", { name: "Connect Mail" }),
      );
      await waitFor(() => expect(connectCalls).toEqual(["connect"]));
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  describe("naming who manages a shared agent", () => {
    const egressRow = (changes: Partial<EgressCredentialService> = {}) =>
      credentialService({
        name: "openai",
        display_name: "OpenAI",
        description: "OpenAI API",
        is_shared: true,
        can_manage: false,
        configured: true,
        active_source: "key",
        key_configured: true,
        ...changes,
      });
    const catalogWith = (
      agent: Record<string, unknown>,
      serviceChanges: Record<string, unknown> = {},
    ) => ({
      agents: [
        {
          ...sharedCatalog([egressRow()]).agents[0],
          ...agent,
          services: [{ ...service, is_shared: true, ...serviceChanges }],
        },
      ],
    });
    const open = async (data: unknown) => {
      installApi({
        "/api/connections": async () => json(data),
        [statusPath]: async () => json({ ...status, can_connect: false }),
      });
      render(<Connections />);
      await expandAgent("Research Team");
    };

    it("tells a non-manager which managers can change the keys, in the key section and beside the account", async () => {
      await open(
        catalogWith(
          { managers: ["@alice:example.org", "@mallory:example.org"] },
          { can_manage: false },
        ),
      );
      const note =
        "Only @alice:example.org and @mallory:example.org can change keys for this shared agent.";
      // Once under the API keys heading, once where the account would be connected.
      await waitFor(() => expect(screen.getAllByText(note)).toHaveLength(2));
    });

    it("tells a manager they manage the keys", async () => {
      await open({
        agents: [
          {
            ...catalogWith(
              { managers: ["@alice:example.org"] },
              { can_manage: true },
            ).agents[0],
            egress_services: [egressRow({ can_manage: true })],
          },
        ],
      });
      expect(
        await screen.findByText(
          "You manage this shared agent's keys. Everyone who uses it shares them.",
        ),
      ).toBeVisible();
      expect(screen.queryByText(/Only .* can change keys/)).toBeNull();
    });

    it("says administrators when no manager is named", async () => {
      await open(catalogWith({ managers: [] }, { can_manage: false }));
      expect(
        (
          await screen.findAllByText(
            "Only administrators can change keys for this shared agent.",
          )
        ).length,
      ).toBeGreaterThan(0);
    });

    it("does not mention keys beside the account while the agent shows no keys", async () => {
      await open({
        agents: [
          {
            ...catalogWith(
              { managers: ["@alice:example.org", "@mallory:example.org"] },
              { can_manage: false },
            ).agents[0],
            egress_services: null,
          },
        ],
      });
      expect(
        await screen.findByText(
          "Only @alice:example.org and @mallory:example.org can change this shared agent's accounts.",
        ),
      ).toBeVisible();
      expect(screen.queryByText(/keys/)).toBeNull();
    });

    it("says administrators can change the accounts when no manager is named and the agent shows no keys", async () => {
      await open({
        agents: [
          {
            ...catalogWith({ managers: [] }, { can_manage: false }).agents[0],
            egress_services: null,
          },
        ],
      });
      expect(
        await screen.findByText(
          "Only administrators can change this shared agent's accounts.",
        ),
      ).toBeVisible();
    });

    it("names the managers when a personal account is refused on a shared agent", async () => {
      const refused = egressRow({
        name: "github",
        display_name: "GitHub",
        configured: false,
        key_configured: false,
        active_source: null,
        oauth: oauthStatus({
          can_connect: false,
          unavailable_reason: "shared_sandbox",
        }),
      });
      await open({
        agents: [
          {
            ...catalogWith(
              { managers: ["@alice:example.org"] },
              { can_manage: false },
            ).agents[0],
            egress_services: [refused],
          },
        ],
      });
      expect(
        await screen.findByText(
          "Personal GitHub accounts can't be used by shared agents. Ask @alice:example.org to add a team API key.",
        ),
      ).toBeVisible();
    });
  });

  it("lists every service of the agent that uses the provider when disconnecting", async () => {
    const egress = (name: string, display_name: string, provider: string) =>
      credentialService({
        name,
        display_name,
        description: "",
        configured: true,
        active_source: "oauth",
        oauth: oauthStatus({ provider, display_name: "Mail", connected: true }),
      });
    installApi({
      "/api/connections": async () =>
        json(
          withEgress([
            egress("mail_api", "Mail API", "mail"),
            egress("mail_labels", "Mail labels", "mail"),
            egress("calendar", "Calendar", "calendar"),
          ]),
        ),
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, connected: true }),
    });
    render(<Connections />);
    await expandAgent();
    const tools = await screen.findByRole("table", {
      name: "Personal assistant tools",
    });
    fireEvent.click(
      await within(tools).findByRole("button", { name: "Disconnect for Mail" }),
    );
    expect(
      within(
        screen.getByRole("dialog", { name: "Disconnect Mail?" }),
      ).getByText(
        "This removes the saved Mail connection for Personal assistant. These services stop working until an account is connected again: Mail API, Mail labels, and Mail.",
      ),
    ).toBeVisible();
  });
});

describe("agent table summary, search, and order", () => {
  const egress = (
    name: string,
    display_name: string,
    overrides: Partial<EgressCredentialService> = {},
  ) => credentialService({ name, display_name, description: "", ...overrides });
  const agentWith = (name: string, display_name: string, extra = {}) => ({
    ...catalog([service]).agents[0],
    agent_name: name,
    agent_display_name: display_name,
    ...extra,
  });
  const agentRow = (name: string) =>
    screen
      .getByRole("button", { name: new RegExp(`^(Expand|Collapse) ${name}$`) })
      .closest("tr")!;
  const withCalculator = () => {
    const base = agentWith("personal", "Personal assistant");
    return {
      ...base,
      tools: [
        ...base.tools,
        {
          name: "calculator",
          display_name: "Calculator",
          description: "Calculate.",
          provider: null,
          icon: null,
          requires_room_context: false,
        },
      ],
    };
  };

  it("counts the accounts and keys that are set against all of them", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            {
              ...withCalculator(),
              services: [{ ...service, connected: true }],
              egress_services: [
                egress("github", "GitHub", {
                  configured: true,
                  key_configured: true,
                  active_source: "key",
                }),
                egress("openai", "OpenAI"),
              ],
            },
          ],
        }),
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, connected: true }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Personal assistant" });
    // Mail's account and GitHub's key, before any status is read.
    expect(agentRow("Personal assistant")).toHaveTextContent(
      "2 tools / 2 of 3 accounts and keys set",
    );
    await expandAgent();
    await screen.findByRole("button", { name: "Disconnect for Mail" });
    expect(agentRow("Personal assistant")).toHaveTextContent(
      "2 tools / 2 of 3 accounts and keys set",
    );
  });

  it("counts only accounts while the agent shows no keys", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            {
              ...withCalculator(),
              services: [{ ...service, connected: true }],
              egress_services: null,
            },
          ],
        }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Personal assistant" });
    expect(agentRow("Personal assistant")).toHaveTextContent(
      "2 tools / 1 of 1 account set",
    );
    expect(agentRow("Personal assistant")).not.toHaveTextContent(/keys/);
  });

  it("counts only the tools of an agent with no accounts or keys to set", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            agentWith("personal", "Personal assistant", {
              services: [],
              egress_services: null,
            }),
            agentWith("research", "Research Team", {
              services: [],
              egress_services: [],
            }),
          ],
        }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Personal assistant" });
    for (const name of ["Personal assistant", "Research Team"]) {
      expect(within(agentRow(name)).getByText("1 tool")).toBeInTheDocument();
      expect(agentRow(name)).not.toHaveTextContent(/accounts?|keys?|set/);
    }
  });

  it("does not count a service that only limits paths as an account or a key", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            {
              ...withCalculator(),
              services: [],
              egress_services: [
                egress("github", "GitHub", {
                  configured: true,
                  key_configured: true,
                  active_source: "key",
                }),
                egress("github-limited", "GitHub (a/b)", {
                  source: "user",
                  restrict_only: true,
                }),
              ],
            },
          ],
        }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Personal assistant" });
    expect(agentRow("Personal assistant")).toHaveTextContent(
      "2 tools / 1 of 1 account or key set",
    );
  });

  it("counts an account as it is disconnected", async () => {
    let connected = true;
    installApi({
      "/api/connections": async () =>
        json({ agents: [{ ...withCalculator(), egress_services: [] }] }),
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, connected }),
      "/api/connections/agents/personal/mail/disconnect": async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    await waitFor(() =>
      expect(agentRow("Personal assistant")).toHaveTextContent(
        "2 tools / 1 of 1 account or key set",
      ),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect for Mail" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Disconnect",
      }),
    );
    await waitFor(() =>
      expect(agentRow("Personal assistant")).toHaveTextContent(
        "2 tools / 0 of 1 account or key set",
      ),
    );
  });

  it("counts a connected account from the catalog before the agent is opened, and lets a loaded status correct it", async () => {
    const connected = (agent: ReturnType<typeof withCalculator>) => ({
      ...agent,
      services: agent.services.map((item) => ({ ...item, connected: true })),
      egress_services: [],
    });
    installApi({
      "/api/connections": async () =>
        json({ agents: [connected(withCalculator())] }),
      // The account was disconnected elsewhere since the catalog was read.
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, connected: false }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Personal assistant" });
    expect(agentRow("Personal assistant")).toHaveTextContent(
      "2 tools / 1 of 1 account or key set",
    );
    await expandAgent();
    await waitFor(() =>
      expect(agentRow("Personal assistant")).toHaveTextContent(
        "2 tools / 0 of 1 account or key set",
      ),
    );
  });

  it("follows a refreshed catalog for an agent that was opened and closed, as after a disconnect through another agent", async () => {
    // Both agents use the requester's own Mail account.
    let connected = true;
    const agent = (name: string, display_name: string) =>
      agentWith(name, display_name, {
        services: [{ ...service, connected }],
        egress_services: [],
      });
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            agent("personal", "Personal assistant"),
            agent("research", "Research Team"),
          ],
        }),
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, connected }),
      "/api/connections/agents/research/mail/status": async () =>
        json({ ...status, connected }),
      "/api/connections/agents/research/mail/disconnect": async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
    await expandAgent();
    await screen.findByRole("button", { name: "Disconnect for Mail" });
    fireEvent.click(
      screen.getByRole("button", { name: "Collapse Personal assistant" }),
    );
    await expandAgent("Research Team");
    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect for Mail" }),
    );
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Disconnect",
      }),
    );
    await waitFor(() =>
      expect(agentRow("Research Team")).toHaveTextContent(
        "1 tool / 0 of 1 account or key set",
      ),
    );
    // The closed agent cannot load its status again, so the refreshed catalog decides.
    await waitFor(() =>
      expect(agentRow("Personal assistant")).toHaveTextContent(
        "1 tool / 0 of 1 account or key set",
      ),
    );
  });

  const twoAgents = () => ({
    agents: [
      {
        ...agentWith("research", "Research Team"),
        egress_services: [
          egress("billing", "Billing API", {
            rules: [
              { host: "billing.example.com", port: null, path_prefix: "/" },
            ],
          }),
        ],
      },
      {
        ...agentWith("writer", "Writer"),
        services: [],
        tools: [],
        egress_services: [],
      },
    ],
  });
  const search = (text: string) =>
    fireEvent.change(
      screen.getByRole("searchbox", {
        name: "Search agents, tools, or services",
      }),
      { target: { value: text } },
    );
  const shown = () =>
    screen
      .getAllByRole("button", { name: /^(Expand|Collapse) / })
      .map((button) => button.getAttribute("aria-label"));

  it.each([
    ["a service's display name", "billing api"],
    ["a service's name", "billing"],
    ["a rule's host", "BILLING.example.com"],
    ["an account's display name", "Mail"],
  ])("search finds an agent by %s", async (_what, text) => {
    installApi({ "/api/connections": async () => json(twoAgents()) });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand Research Team" });
    expect(shown()).toEqual(["Expand Research Team", "Expand Writer"]);
    search(text);
    expect(shown()).toEqual(["Expand Research Team"]);
    search("nothing like this");
    expect(screen.getByText("No agents match your search.")).toBeVisible();
  });

  it("keeps the agents in the order the API sends them", async () => {
    const order = ["zeta", "alpha", "mid"];
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            ...order.map((name) => agentWith(name, name.toUpperCase())),
          ].map((agent, index) =>
            // The personal agent is not first, and an agent with a key is not moved up.
            index === 2
              ? {
                  ...agent,
                  egress_services: [egress("k", "Key", { configured: true })],
                }
              : agent,
          ),
        }),
    });
    render(<Connections />);
    await screen.findByRole("button", { name: "Expand ZETA" });
    expect(shown()).toEqual(["Expand ZETA", "Expand ALPHA", "Expand MID"]);
    fireEvent.click(screen.getByRole("button", { name: "Expand MID" }));
    expect(shown()).toEqual(["Expand ZETA", "Expand ALPHA", "Collapse MID"]);
  });

  it("falls back to the agent's icon without a word when its picture is a 204", async () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => {});
    installApi({ "/api/connections": async () => json(twoAgents()) });
    render(<Connections />);
    const button = await screen.findByRole("button", {
      name: "Expand Research Team",
    });
    // An empty 204 body is a broken image to the browser.
    fireEvent.error(button.querySelector("img")!);
    expect(button.querySelector("img")).toBeNull();
    expect(button.querySelector("svg.lucide-user-round")).not.toBeNull();
    expect(consoleError).not.toHaveBeenCalled();
    expect(consoleWarn).not.toHaveBeenCalled();
  });

  it("words an agent's key rows as the personal pages do", async () => {
    installApi({
      "/api/connections": async () =>
        json({
          agents: [
            {
              ...agentWith("personal", "Personal assistant"),
              egress_services: [
                egress("openai", "OpenAI", {
                  configured: true,
                  key_configured: true,
                  active_source: "key",
                  key_updated_at: "2026-10-05T12:00:00+00:00",
                }),
              ],
            },
          ],
        }),
    });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByText(
        `Key saved ${new Date("2026-10-05T12:00:00+00:00").toLocaleDateString()}`,
      ),
    ).toBeInTheDocument();
  });
});

describe("connections header", () => {
  it("shows who is signed in and links the other page", async () => {
    render(<Connections />);
    expect(
      await screen.findByText("Signed in as Bob Builder"),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Connections pages" });
    expect(within(nav).getByRole("link", { name: "Agents" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      within(nav).getByRole("link", { name: "Agent Keys" }),
    ).toHaveAttribute("href", "/connections/egress");
    expect(within(nav).queryByRole("link", { name: "Dashboard" })).toBeNull();
  });

  it("does not link Agent Keys while the egress broker is off", async () => {
    installApi({
      "/api/connections/me": async () =>
        json({
          matrix_user_id: "@bob:example.org",
          display_name: "Bob Builder",
          email: null,
          is_administrator: false,
          egress_enabled: false,
        }),
    });
    render(<Connections />);
    expect(
      await screen.findByText("Signed in as Bob Builder"),
    ).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Connections pages" });
    expect(within(nav).getByRole("link", { name: "Agents" })).toBeVisible();
    expect(within(nav).queryByRole("link", { name: "Agent Keys" })).toBeNull();
  });
});
