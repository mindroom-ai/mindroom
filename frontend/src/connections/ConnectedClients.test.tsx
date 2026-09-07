import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectedClients } from "./ConnectedClients";

const client = {
  id: "grant/one",
  client_name: "Desktop tools",
  redirect_uri: "https://desktop.example.test/oauth/callback?source=portal",
  created_at: 1_735_689_600,
  last_used_at: null,
  idle_expires_at: 1_738_281_600,
  expires_at: 1_751_241_600,
};

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

function installApi(
  handler: (path: string, options?: RequestInit) => Promise<Response>,
) {
  vi.mocked(fetch).mockImplementation((input, options) =>
    handler(String(input), options),
  );
}

beforeEach(() => {
  installApi(async () =>
    json({ enabled: true, clients: [client], next_cursor: null }),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("connected clients", () => {
  it("shows a loading state until the client list arrives", async () => {
    let finish!: (response: Response) => void;
    installApi(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );

    render(<ConnectedClients />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading connected clients",
    );
    finish(json({ enabled: true, clients: [], next_cursor: null }));
    expect(await screen.findByText("No clients are connected.")).toBeVisible();
  });

  it("lists each client with its redirect host and lifecycle dates", async () => {
    const usedClient = {
      ...client,
      id: "grant-used",
      client_name: "Used tools",
      redirect_uri: "https://used.example.test/callback",
      last_used_at: 1_735_776_000,
    };
    installApi(async () =>
      json({
        enabled: true,
        clients: [client, usedClient],
        next_cursor: null,
      }),
    );
    const { container } = render(<ConnectedClients />);

    expect(
      await screen.findByRole("heading", { name: "Desktop tools" }),
    ).toBeVisible();
    expect(screen.getByText("desktop.example.test")).toBeVisible();
    expect(screen.getByText("Never used")).toBeVisible();
    expect(
      container.querySelector('time[datetime="2025-01-02T00:00:00.000Z"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('time[datetime="2025-01-31T00:00:00.000Z"]'),
    ).toBeInTheDocument();
    expect(
      container.querySelector('time[datetime="2025-06-30T00:00:00.000Z"]'),
    ).toBeInTheDocument();
  });

  it("shows unknown when a migrated client's usage history was not recorded", async () => {
    installApi(async () =>
      json({
        enabled: true,
        clients: [{ ...client, created_at: null, last_used_at: null }],
        next_cursor: null,
      }),
    );

    render(<ConnectedClients />);

    expect(await screen.findByText("Unknown")).toBeVisible();
    expect(screen.queryByText("Never used")).not.toBeInTheDocument();
  });

  it("does not invent callback details for a migrated client", async () => {
    installApi(async () =>
      json({
        enabled: true,
        clients: [
          { ...client, client_name: "Legacy tools", redirect_uri: null },
        ],
        next_cursor: null,
      }),
    );

    render(<ConnectedClients />);

    expect(await screen.findByText("Client address unavailable")).toBeVisible();
    expect(screen.queryByText("desktop.example.test")).not.toBeInTheDocument();
  });

  it("shows an empty state when no clients are connected", async () => {
    installApi(async () =>
      json({ enabled: true, clients: [], next_cursor: null }),
    );

    render(<ConnectedClients />);

    expect(await screen.findByText("No clients are connected.")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Disconnect all" }),
    ).not.toBeInTheDocument();
  });

  it("hides the section when client connections are disabled", async () => {
    installApi(async () => json({ enabled: false, clients: [] }));

    render(<ConnectedClients />);

    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Connected clients" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("shows a safe list error without displaying server details", async () => {
    installApi(async () => json({ detail: "Sensitive server detail" }, 500));

    render(<ConnectedClients />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load connected clients",
    );
    expect(
      screen.queryByText("Sensitive server detail"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("No clients are connected."),
    ).not.toBeInTheDocument();
  });

  it("loads later pages with an encoded cursor and appends unique grants", async () => {
    const secondClient = {
      ...client,
      id: "grant-two",
      client_name: "Mobile tools",
      redirect_uri: "https://mobile.example.test/callback",
    };
    installApi(async (path) => {
      if (path === "/api/connections/mcp/clients")
        return json({
          enabled: true,
          clients: [client],
          next_cursor: "after/one",
        });
      expect(path).toBe("/api/connections/mcp/clients?cursor=after%2Fone");
      return json({
        enabled: true,
        clients: [client, secondClient],
        next_cursor: null,
      });
    });
    render(<ConnectedClients />);

    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));

    expect(
      await screen.findByRole("heading", { name: "Mobile tools" }),
    ).toBeVisible();
    expect(
      screen.getAllByRole("heading", { name: "Desktop tools" }),
    ).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "Load more" }),
    ).not.toBeInTheDocument();
  });

  it("revokes exactly one grant and removes its row after success", async () => {
    const paths: string[] = [];
    let revoked = false;
    installApi(async (path, options) => {
      paths.push(path);
      if (options?.method !== "POST")
        return json({
          enabled: true,
          clients: revoked ? [] : [client],
          next_cursor: null,
        });
      expect(path).toBe("/api/connections/mcp/clients/grant%2Fone/revoke");
      expect(options).toMatchObject({
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      revoked = true;
      return json({ success: true });
    });
    render(<ConnectedClients />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Disconnect Desktop tools from desktop.example.test",
      }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: "Desktop tools" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("No clients are connected.")).toBeVisible();
    expect(paths).toEqual([
      "/api/connections/mcp/clients",
      "/api/connections/mcp/clients/grant%2Fone/revoke",
      "/api/connections/mcp/clients",
    ]);
  });

  it("keeps a client visible when its revoke request fails", async () => {
    installApi(async (path) => {
      if (path === "/api/connections/mcp/clients")
        return json({ enabled: true, clients: [client], next_cursor: null });
      return json({ detail: "Sensitive server detail" }, 500);
    });
    render(<ConnectedClients />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Disconnect Desktop tools from desktop.example.test",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not disconnect this client",
    );
    expect(
      screen.getByRole("heading", { name: "Desktop tools" }),
    ).toBeVisible();
    expect(
      screen.queryByText("Sensitive server detail"),
    ).not.toBeInTheDocument();
  });

  it("keeps unaffected clients when refresh fails after a successful revoke", async () => {
    const secondClient = {
      ...client,
      id: "grant-two",
      client_name: "Mobile tools",
      redirect_uri: "https://mobile.example.test/callback",
    };
    let revoked = false;
    installApi(async (path, options) => {
      if (options?.method === "POST") {
        revoked = true;
        return json({ success: true });
      }
      if (revoked) return json({ detail: "Refresh failed" }, 500);
      expect(path).toBe("/api/connections/mcp/clients");
      return json({
        enabled: true,
        clients: [client, secondClient],
        next_cursor: null,
      });
    });
    render(<ConnectedClients />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Disconnect Desktop tools from desktop.example.test",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load connected clients",
    );
    expect(
      screen.queryByRole("heading", { name: "Desktop tools" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Mobile tools" })).toBeVisible();
    expect(
      screen.queryByText("No clients are connected."),
    ).not.toBeInTheDocument();
  });

  it("does not claim empty when an unloaded page remains after refresh failure", async () => {
    let revoked = false;
    installApi(async (_path, options) => {
      if (options?.method === "POST") {
        revoked = true;
        return json({ success: true });
      }
      if (revoked) return json({ detail: "Refresh failed" }, 500);
      return json({
        enabled: true,
        clients: [client],
        next_cursor: "more-clients",
      });
    });
    render(<ConnectedClients />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Disconnect Desktop tools from desktop.example.test",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load connected clients",
    );
    expect(
      screen.queryByRole("heading", { name: "Desktop tools" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("No clients are connected."),
    ).not.toBeInTheDocument();
  });

  it("requires confirmation before disconnecting all clients", async () => {
    const secondClient = {
      ...client,
      id: "grant-two",
      client_name: "Mobile tools",
      redirect_uri: "https://mobile.example.test/callback",
    };
    const postPaths: string[] = [];
    let revoked = false;
    installApi(async (path, options) => {
      if (path === "/api/connections/mcp/clients")
        return json({
          enabled: true,
          clients: revoked ? [] : [client, secondClient],
          next_cursor: revoked ? null : "more-clients",
        });
      if (options?.method === "POST") {
        postPaths.push(path);
        revoked = true;
      }
      return json({ success: true });
    });
    render(<ConnectedClients />);

    fireEvent.click(
      await screen.findByRole("button", { name: "Disconnect all" }),
    );
    const firstDialog = screen.getByRole("dialog");
    expect(firstDialog).toHaveTextContent("all connected clients");
    expect(firstDialog).toHaveTextContent(
      "Your connected service accounts will stay connected",
    );
    fireEvent.click(
      within(firstDialog).getByRole("button", { name: "Cancel" }),
    );
    expect(postPaths).toEqual([]);
    expect(
      screen.getByRole("heading", { name: "Desktop tools" }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Disconnect all" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Disconnect all clients",
      }),
    );

    await waitFor(() =>
      expect(postPaths).toEqual(["/api/connections/mcp/clients/revoke-all"]),
    );
    expect(await screen.findByText("No clients are connected.")).toBeVisible();
  });

  it("aborts its list request when unmounted", () => {
    let requestSignal: AbortSignal | undefined;
    installApi((_path, options) => {
      requestSignal = options?.signal ?? undefined;
      return new Promise(() => {});
    });

    const { unmount } = render(<ConnectedClients />);
    unmount();

    expect(requestSignal?.aborted).toBe(true);
  });

  it("renders self-declared metadata as text and exposes only the redirect host", async () => {
    const unsafeName = '<img src="x" onerror="alert(1)">';
    const unsafeRedirect =
      "https://safe.example.test/callback?<script>alert(1)</script>";
    installApi(async () =>
      json({
        enabled: true,
        clients: [
          { ...client, client_name: unsafeName, redirect_uri: unsafeRedirect },
        ],
        next_cursor: null,
      }),
    );
    const { container } = render(<ConnectedClients />);

    expect(await screen.findByText(unsafeName)).toBeVisible();
    expect(screen.getByText("safe.example.test")).toBeVisible();
    expect(screen.queryByText(unsafeRedirect)).not.toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
    expect(container.querySelector("script")).not.toBeInTheDocument();
  });
});
