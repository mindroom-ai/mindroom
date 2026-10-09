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
import type { EgressCredentialAgent } from "./types";

const service = {
  name: "github",
  display_name: "GitHub",
  description: "GitHub API",
  is_shared: false,
  can_manage: true,
  configured: false,
  updated_at: null,
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
            services: [{ ...service, configured }],
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
});
