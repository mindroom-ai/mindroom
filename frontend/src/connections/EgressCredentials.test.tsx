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
  EgressCredentialAgent,
  EgressCredentialService,
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
    expect(within(shared).queryByRole("button")).toBeNull();
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
