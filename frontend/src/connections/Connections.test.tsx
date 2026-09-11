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
import { Connections } from "./Connections";
import type { ConnectionService } from "./types";

const service: ConnectionService = {
  icon: null,
  provider: "mail",
  is_shared: false,
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
};
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });

const catalog = (services: (typeof service)[]) => ({
  agents: [
    {
      agent_name: "personal",
      agent_display_name: "Personal assistant",
      is_shared: false,
      services,
      tools: services.flatMap((item) =>
        item.tools.map((name) => ({
          name,
          display_name: item.display_name,
          description: item.description,
          provider: item.provider,
          requires_room_context: false,
          icon: item.icon,
        })),
      ),
    },
  ],
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
      await screen.findByRole("button", { name: "Disconnect Calendar" }),
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
      screen.getByRole("button", { name: "Disconnect Calendar" }),
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
      await screen.findByRole("button", { name: "Disconnect Mail" }),
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
      await screen.findByRole("button", { name: "Reset Mail connection" }),
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

  it("opens popup before requesting authorization and reloads authoritative status", async () => {
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
    };
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(popup as unknown as Window);
    let connected = false;
    installApi({
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
            type: "mindroom:oauth-complete",
            provider: "mail",
            status: "connected",
          },
        }),
      ),
    );
    expect(
      await screen.findByRole("button", { name: "Disconnect Mail" }),
    ).toBeEnabled();
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

  it("keeps an unavailable provider disabled", async () => {
    installApi({
      "/api/connections/agents/personal/mail/status": async () =>
        json({ ...status, can_connect: false }),
    });
    render(<Connections />);
    await expandAgent();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeDisabled();
    expect(screen.getByText(/not ready to connect/i)).toBeInTheDocument();
  });

  it("closes an in-flight popup when the portal unmounts", async () => {
    const popup = {
      closed: false,
      close: vi.fn(),
      location: { href: "about:blank" },
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
      await within(personal).findByRole("button", { name: "Disconnect Mail" }),
    ).toBeEnabled();
    fireEvent.click(
      await within(shared).findByRole("button", { name: "Disconnect Mail" }),
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
      await within(shared).findByRole("button", { name: "Disconnect Mail" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Research Team");
    expect(dialog).toHaveTextContent(/anyone using this connection/i);
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

it("describes a requester-only connection on a shared agent as personal", async () => {
  installApi({
    "/api/connections": async () =>
      json({
        agents: [
          {
            agent_name: "research",
            agent_display_name: "Research Team",
            is_shared: true,
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
    await screen.findByRole("button", { name: "Disconnect Mail" }),
  );
  const dialog = screen.getByRole("dialog");
  expect(dialog).toHaveTextContent(/your saved connection/i);
  expect(dialog).not.toHaveTextContent(/anyone using this connection/i);
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
            requires_room_context: false,
          },
          {
            name: "matrix_message",
            display_name: "Matrix Message",
            description: "Send room messages.",
            provider: null,
            icon: null,
            requires_room_context: true,
          },
        ],
      },
      {
        agent_name: "shared",
        agent_display_name: "Research Team",
        is_shared: true,
        services: [],
        tools: [
          {
            name: "web",
            display_name: "Web Search",
            description: "Search the web.",
            provider: null,
            icon: null,
            requires_room_context: false,
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
      name: "Search agents or tools",
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
