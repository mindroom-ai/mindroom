import {
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

const github = {
  name: "github",
  display_name: "GitHub",
  description: "GitHub API and git over HTTPS",
  configured: false,
  updated_at: null,
};
const openai = {
  name: "openai",
  display_name: null,
  description: "OpenAI API",
  configured: true,
  updated_at: "2026-10-05T12:00:00+00:00",
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

let logsResponse: () => Response;

function mockApi() {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    if (url.pathname === "/api/egress-broker/services" && method === "GET")
      return json({ services: [github, openai] });
    if (url.pathname === "/api/egress-broker/logs") return logsResponse();
    return new Response(null, { status: 204 });
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

beforeEach(() => {
  logsResponse = () => json({ records: [newest, oldest] });
  mockApi();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("egress broker panel", () => {
  it("lists the services with their status", async () => {
    render(<EgressBroker agentName={null} />);

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
    render(<EgressBroker agentName={null} />);

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
    render(<EgressBroker agentName={null} />);

    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("saves a secret for the selected agent and clears the input", async () => {
    render(<EgressBroker agentName="coder" />);
    await screen.findByText("GitHub");
    expect(
      requests("/api/egress-broker/services")[0].searchParams.get("agent_name"),
    ).toBe("coder");

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
      expect(requests("/api/egress-broker/services")).toHaveLength(2),
    );
    // Reopening the editor must not show the previous value.
    fireEvent.click(screen.getByRole("button", { name: "Set GitHub API key" }));
    expect(screen.getByLabelText("GitHub API key")).toHaveValue("");
  });

  it("saves a global secret without an agent_name", async () => {
    render(<EgressBroker agentName={null} />);
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

  it("removes a secret after confirmation", async () => {
    render(<EgressBroker agentName="coder" />);
    await screen.findByText("GitHub");

    fireEvent.click(
      screen.getByRole("button", { name: "Remove openai API key" }),
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

  it("shows the request log newest first with local times", async () => {
    render(<EgressBroker agentName={null} />);

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
    render(<EgressBroker agentName={null} />);

    expect(
      await screen.findByText("No requests logged yet."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("filters the log by agent, host and service on refresh", async () => {
    render(<EgressBroker agentName={null} />);
    await screen.findByRole("table", { name: "Request log" });
    expect(requests("/api/egress-broker/logs")[0].search).toBe("");

    fireEvent.change(screen.getByLabelText("Agent"), {
      target: { value: "coder" },
    });
    fireEvent.change(screen.getByLabelText("Host"), {
      target: { value: "api.github.com" },
    });
    fireEvent.change(screen.getByLabelText("Service"), {
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

  it("replaces the log table with the not-running message on 409", async () => {
    logsResponse = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressBroker agentName={null} />);

    expect(await screen.findByText(NOT_RUNNING)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    // Services stay manageable while the broker is down.
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("shows a generic error when the log request fails", async () => {
    logsResponse = () => json({ detail: "boom" }, 500);
    render(<EgressBroker agentName={null} />);

    expect(
      await screen.findByText("Could not load the request log. Try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText(NOT_RUNNING)).toBeNull();
  });

  it("recovers once the broker is running and the log is refreshed", async () => {
    logsResponse = () => json({ detail: "Egress broker is not running" }, 409);
    render(<EgressBroker agentName={null} />);
    await screen.findByText(NOT_RUNNING);

    logsResponse = () => json({ records: [newest] });
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect(
      await screen.findByRole("table", { name: "Request log" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(NOT_RUNNING)).toBeNull();
  });

  it("links to the CA certificate download", async () => {
    render(<EgressBroker agentName={null} />);

    const link = await screen.findByRole("link", {
      name: "Download CA certificate",
    });
    expect(link).toHaveAttribute("href", "/api/egress-broker/ca.pem");
    expect(link).toHaveAttribute("download");
  });
});
