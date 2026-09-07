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

const service = {
  provider: "mail",
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

function installApi(overrides: Record<string, () => Promise<Response>> = {}) {
  vi.mocked(fetch).mockImplementation(async (input, options) => {
    const path = String(input);
    if (overrides[path]) return overrides[path]();
    if (path === "/api/connections")
      return json({
        agent_display_name: "Personal assistant",
        services: [service],
      });
    if (path === "/api/connections/mail/status") return json(status);
    if (path.endsWith("/disconnect")) {
      expect(options).toMatchObject({ method: "POST", body: "{}" });
      return json({ status: "disconnected", provider: "mail" });
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

beforeEach(() => installApi());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("personal connections", () => {
  it("shows only the server's services without loading dashboard configuration", async () => {
    render(<Connections />);
    expect(
      await screen.findByRole("heading", { name: "Mail" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Personal assistant")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Connect Mail" }),
    ).toBeEnabled();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.map(([url]) => url)).toEqual([
      "/api/connections",
      "/api/connections/mail/status",
    ]);
  });

  it("loads statuses concurrently and isolates a failed provider", async () => {
    let finishMail!: (response: Response) => void;
    installApi({
      "/api/connections": async () =>
        json({
          agent_display_name: "Personal assistant",
          services: [
            service,
            { ...service, provider: "calendar", display_name: "Calendar" },
          ],
        }),
      "/api/connections/mail/status": () =>
        new Promise((resolve) => {
          finishMail = resolve;
        }),
      "/api/connections/calendar/status": async () =>
        json({
          ...status,
          provider: "calendar",
          connected: true,
          account_label: "user@example.com",
        }),
    });
    render(<Connections />);
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

  it("requires confirmation before disconnecting, then refreshes status", async () => {
    let connected = true;
    installApi({
      "/api/connections/mail/status": async () =>
        json({
          ...status,
          connected,
          account_label: connected ? "user@example.com" : null,
        }),
      "/api/connections/mail/disconnect": async () => {
        connected = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
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
      "/api/connections/mail/status": async () =>
        json({ ...status, reset_required: resetRequired }),
      "/api/connections/mail/disconnect": async () => {
        resetRequired = false;
        return json({ status: "disconnected", provider: "mail" });
      },
    });
    render(<Connections />);
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
      "/api/connections/mail/status": async () =>
        json({ ...status, connected }),
      "/api/connections/mail/connect": async () => {
        expect(open).toHaveBeenCalledOnce();
        return json({
          provider: "mail",
          auth_url: "https://auth.example.com/start",
          completion_origin: window.location.origin,
        });
      },
    });
    render(<Connections />);
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
      "/api/connections": async () =>
        json({ agent_display_name: "Personal assistant", services: [] }),
    });
    render(<Connections />);
    expect(
      await screen.findByText(/no services.*available/i),
    ).toBeInTheDocument();
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
      "/api/connections/mail/status": async () =>
        json({ ...status, can_connect: false }),
    });
    render(<Connections />);
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
      "/api/connections/mail/connect": () => new Promise(() => {}),
    });
    const { unmount } = render(<Connections />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Connect Mail" }),
    );
    unmount();
    await waitFor(() => expect(popup.close).toHaveBeenCalledOnce());
  });
});
