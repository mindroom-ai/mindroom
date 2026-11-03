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
import { ConsentGroupConnect } from "./ConsentGroupConnect";
import type {
  AgentConnections,
  ConnectionService,
  ConnectionStatus,
} from "./types";

const google = { key: "google", display_name: "Google" };
const service = (
  provider: string,
  consent_group: ConnectionService["consent_group"] = google,
  overrides: Partial<ConnectionService> = {},
): ConnectionService => ({
  provider,
  is_shared: false,
  can_manage: true,
  display_name: provider,
  description: "",
  icon: null,
  tools: [provider],
  consent_group,
  ...overrides,
});
const agentWith = (
  services: ConnectionService[],
  overrides: Partial<AgentConnections> = {},
): AgentConnections => ({
  agent_name: "personal",
  agent_display_name: "Personal assistant",
  is_shared: false,
  can_use: true,
  managers: [],
  mcp_available: true,
  services,
  tools: [],
  ...overrides,
});
const unconnected = (provider: string): ConnectionStatus => ({
  provider,
  connected: false,
  can_connect: true,
  reset_required: false,
  account_label: null,
  service_account: false,
});
const statusesFor = (...providers: string[]) =>
  Object.fromEntries(
    providers.map((provider) => [provider, unconnected(provider)]),
  );
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });

const googleAgent = () =>
  agentWith([service("google_drive"), service("google_sheets")]);
const GROUP_PATH =
  "/api/connections/agents/personal/consent-groups/google/connect";

const popup = () => ({
  closed: false,
  close: vi.fn(),
  location: { href: "about:blank" },
  document: document.implementation.createHTMLDocument(),
});
const answer = (
  window_: ReturnType<typeof popup>,
  provider: string,
  status: "connected" | "cancelled",
) =>
  act(() =>
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: window.location.origin,
        source: window_ as unknown as Window,
        data: { type: "mindroom-oauth", provider, status },
      }),
    ),
  );

const renderGroup = (
  agent: AgentConnections,
  statuses: Record<string, ConnectionStatus>,
) => {
  const onConnectionChange = vi.fn();
  render(
    <ConsentGroupConnect
      agent={agent}
      statuses={statuses}
      onConnectionChange={onConnectionChange}
    />,
  );
  return { onConnectionChange };
};

const connectAll = () =>
  screen.getByRole("button", { name: "Connect all Google services" });

let fetchMock: ReturnType<typeof vi.mocked<typeof fetch>>;
beforeEach(() => {
  fetchMock = vi.mocked(fetch);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Connect all", () => {
  it("offers one Connect all button for two unconnected Google services", () => {
    renderGroup(googleAgent(), statusesFor("google_drive", "google_sheets"));
    expect(connectAll()).toBeVisible();
    expect(
      screen.getByText("2 Google services are not connected."),
    ).toBeVisible();
    expect(screen.getAllByRole("button", { name: /Connect all/ })).toHaveLength(
      1,
    );
  });

  it("offers nothing when only one service of the group is unconnected", () => {
    renderGroup(googleAgent(), {
      google_drive: { ...unconnected("google_drive"), connected: true },
      google_sheets: unconnected("google_sheets"),
    });
    expect(screen.queryByRole("button", { name: /Connect all/ })).toBeNull();
  });

  it("offers nothing when the other services cannot be connected here", () => {
    const agent = agentWith([
      service("google_drive"),
      service("google_docs", google, { can_manage: false }),
      service("google_gmail"),
      service("google_tasks"),
      service("google_calendar"),
      service("mail", null),
    ]);
    renderGroup(agent, {
      ...statusesFor("google_drive", "google_docs", "mail"),
      google_gmail: { ...unconnected("google_gmail"), reset_required: true },
      google_tasks: { ...unconnected("google_tasks"), can_connect: false },
      // google_calendar has no loaded status yet.
    });
    expect(screen.queryByRole("button", { name: /Connect all/ })).toBeNull();
  });

  it("counts only the services that can be connected here", () => {
    const agent = agentWith([
      service("google_drive"),
      service("google_docs", google, { can_manage: false }),
      service("google_sheets"),
      service("google_tasks"),
      service("mail", null),
    ]);
    renderGroup(agent, {
      ...statusesFor("google_drive", "google_docs", "google_sheets", "mail"),
      google_tasks: { ...unconnected("google_tasks"), reset_required: true },
    });
    expect(
      screen.getByText("2 Google services are not connected."),
    ).toBeVisible();
  });

  it("offers one row per consent group", () => {
    const work = { key: "google_work", display_name: "Google Work" };
    const agent = agentWith([
      service("google_drive"),
      service("google_sheets"),
      service("work_drive", work),
      service("work_sheets", work),
      service("work_docs", work),
    ]);
    renderGroup(
      agent,
      statusesFor(
        "google_drive",
        "google_sheets",
        "work_drive",
        "work_sheets",
        "work_docs",
      ),
    );
    expect(connectAll()).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Connect all Google Work services" }),
    ).toBeVisible();
    expect(
      screen.getByText("2 Google services are not connected."),
    ).toBeVisible();
    expect(
      screen.getByText("3 Google Work services are not connected."),
    ).toBeVisible();
  });

  it("shows a logo only on the button of a built-in group", () => {
    const plugin = { key: "acme", display_name: "Acme" };
    const agent = agentWith([
      service("google_drive"),
      service("google_sheets"),
      service("acme_files", plugin),
      service("acme_notes", plugin),
    ]);
    renderGroup(
      agent,
      statusesFor("google_drive", "google_sheets", "acme_files", "acme_notes"),
    );
    expect(within(connectAll()).getByTestId("group-logo")).toBeTruthy();
    expect(
      within(
        screen.getByRole("button", { name: "Connect all Acme services" }),
      ).queryByTestId("group-logo"),
    ).toBeNull();
  });

  it("starts the group sign-in and reloads statuses", async () => {
    const window_ = popup();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue(window_ as unknown as Window);
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      if (path === GROUP_PATH) {
        expect(open).toHaveBeenCalledOnce();
        return json({
          provider: "google_drive",
          auth_url: "https://auth.example.com/start",
          completion_origin: window.location.origin,
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    await waitFor(() =>
      expect(window_.location.href).toBe("https://auth.example.com/start"),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/consent-groups/google/connect"),
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
    expect(onConnectionChange).not.toHaveBeenCalled();
    answer(window_, "google_drive", "connected");
    await waitFor(() => expect(onConnectionChange).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("treats a popup that closed after connecting as a connection", async () => {
    const window_ = popup();
    vi.spyOn(window, "open").mockReturnValue(window_ as unknown as Window);
    fetchMock.mockImplementation(async (input) => {
      const path = String(input);
      if (path === GROUP_PATH)
        return json({
          provider: "google_sheets",
          auth_url: "https://auth.example.com/start",
          completion_origin: window.location.origin,
        });
      if (path === "/api/connections/agents/personal/google_sheets/status")
        return json({ ...unconnected("google_sheets"), connected: true });
      throw new Error(`Unexpected request: ${path}`);
    });
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    await waitFor(() =>
      expect(window_.location.href).toBe("https://auth.example.com/start"),
    );
    window_.closed = true;
    await waitFor(() => expect(onConnectionChange).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("follows the service the server picks to lead the sign-in", async () => {
    const window_ = popup();
    vi.spyOn(window, "open").mockReturnValue(window_ as unknown as Window);
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === GROUP_PATH)
        return json({
          provider: "google_sheets",
          auth_url: "https://auth.example.com/start",
          completion_origin: window.location.origin,
        });
      throw new Error(`Unexpected request: ${String(input)}`);
    });
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    await waitFor(() =>
      expect(window_.location.href).toBe("https://auth.example.com/start"),
    );
    answer(window_, "google_drive", "connected");
    expect(onConnectionChange).not.toHaveBeenCalled();
    answer(window_, "google_sheets", "connected");
    await waitFor(() => expect(onConnectionChange).toHaveBeenCalledOnce());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the server's refusal and reloads statuses", async () => {
    vi.spyOn(window, "open").mockReturnValue(popup() as unknown as Window);
    fetchMock.mockImplementation(async () =>
      json({ detail: "Nothing in this group can be connected here" }, 409),
    );
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    expect(
      await screen.findByText("Nothing in this group can be connected here"),
    ).toBeVisible();
    expect(onConnectionChange).toHaveBeenCalledOnce();
  });

  it("reports a cancelled sign-in and reloads statuses", async () => {
    const window_ = popup();
    vi.spyOn(window, "open").mockReturnValue(window_ as unknown as Window);
    fetchMock.mockImplementation(async () =>
      json({
        provider: "google_drive",
        auth_url: "https://auth.example.com/start",
        completion_origin: window.location.origin,
      }),
    );
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    await waitFor(() =>
      expect(window_.location.href).toBe("https://auth.example.com/start"),
    );
    answer(window_, "google_drive", "cancelled");
    expect(
      await screen.findByText("Authorization was cancelled."),
    ).toBeVisible();
    expect(onConnectionChange).toHaveBeenCalledOnce();
  });

  it("changes nothing when Cancel abandons the sign-in", async () => {
    const window_ = popup();
    vi.spyOn(window, "open").mockReturnValue(window_ as unknown as Window);
    fetchMock.mockImplementation(async () => new Promise<Response>(() => {}));
    const { onConnectionChange } = renderGroup(
      googleAgent(),
      statusesFor("google_drive", "google_sheets"),
    );
    fireEvent.click(connectAll());
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(window_.close).toHaveBeenCalled());
    expect(connectAll()).toBeEnabled();
    expect(onConnectionChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("disables Connect all while the sign-in is under way", async () => {
    vi.spyOn(window, "open").mockReturnValue(popup() as unknown as Window);
    fetchMock.mockImplementation(async () => new Promise<Response>(() => {}));
    renderGroup(googleAgent(), statusesFor("google_drive", "google_sheets"));
    fireEvent.click(connectAll());
    expect(
      await screen.findByRole("button", { name: "Connecting…" }),
    ).toBeDisabled();
  });

  describe("on a shared agent", () => {
    const shared = () =>
      agentWith([
        service("google_drive", google, { is_shared: true }),
        service("google_sheets", google, { is_shared: true }),
      ]);

    it("asks for shared-account confirmation once on a shared agent", async () => {
      const open = vi
        .spyOn(window, "open")
        .mockReturnValue(popup() as unknown as Window);
      fetchMock.mockImplementation(async () => new Promise<Response>(() => {}));
      renderGroup(shared(), statusesFor("google_drive", "google_sheets"));
      fireEvent.click(connectAll());
      const dialog = screen.getByRole("dialog", {
        name: "Connect a shared account?",
      });
      expect(dialog).toBeVisible();
      expect(
        within(dialog).getByText(
          "Everyone who uses Personal assistant will act as the Google account you pick. Use a team account, not your personal one.",
        ),
      ).toBeVisible();
      expect(open).not.toHaveBeenCalled();
      fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          expect.stringContaining(GROUP_PATH),
          expect.objectContaining({ method: "POST" }),
        ),
      );
      expect(open).toHaveBeenCalledOnce();
    });

    it("sends nothing when the question is cancelled", async () => {
      const open = vi
        .spyOn(window, "open")
        .mockReturnValue(popup() as unknown as Window);
      const { onConnectionChange } = renderGroup(
        shared(),
        statusesFor("google_drive", "google_sheets"),
      );
      fireEvent.click(connectAll());
      fireEvent.click(
        within(screen.getByRole("dialog")).getByRole("button", {
          name: "Cancel",
        }),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(open).not.toHaveBeenCalled();
      expect(fetchMock).not.toHaveBeenCalled();
      expect(onConnectionChange).not.toHaveBeenCalled();
    });
  });

  it("does not ask when no candidate is shared", () => {
    vi.spyOn(window, "open").mockReturnValue(popup() as unknown as Window);
    fetchMock.mockImplementation(async () => new Promise<Response>(() => {}));
    renderGroup(googleAgent(), statusesFor("google_drive", "google_sheets"));
    fireEvent.click(connectAll());
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
