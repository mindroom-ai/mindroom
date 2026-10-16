import { act, cleanup, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const json = (value: unknown) => new Response(JSON.stringify(value));

async function loadEntry(pathname: string) {
  // An explicit theme keeps ThemeProvider from needing matchMedia in jsdom.
  localStorage.setItem("theme", "light");
  document.body.innerHTML = '<div id="root"></div>';
  window.history.pushState({}, "", pathname);
  vi.resetModules();
  await act(async () => {
    await import("./main");
  });
}

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = String(input);
    if (path === "/api/connections/egress") return json({ agents: [] });
    if (path === "/api/connections") return json({ agents: [] });
    if (path === "/api/connections/session")
      return json({ matrix_user_id: "@bob:example.org" });
    if (path === "/api/connections/me")
      return json({
        matrix_user_id: "@bob:example.org",
        email: null,
        is_administrator: false,
        egress_enabled: true,
      });
    if (path === "/api/connections/mcp/selection")
      return json({ enabled: false, agents: {} });
    if (path === "/api/connections/mcp/clients")
      return json({ enabled: false, clients: [] });
    throw new Error(`Unexpected request: ${path}`);
  });
});
afterEach(() => {
  cleanup();
  document.body.innerHTML = "";
  window.history.pushState({}, "", "/");
  vi.restoreAllMocks();
});

it.each([
  "/connections/egress",
  "/connections/egress/",
  "/connections/egress/x",
])("renders the egress page on %s", async (pathname) => {
  await loadEntry(pathname);
  expect(
    await screen.findByRole("heading", { name: "Agent Keys" }),
  ).toBeInTheDocument();
});

it("signs the egress page in, so its requests name the account it shows", async () => {
  await loadEntry("/connections/egress");
  await screen.findByRole("heading", { name: "Agent Keys" });
  const egress = vi
    .mocked(fetch)
    .mock.calls.filter(
      ([input]) => String(input) === "/api/connections/egress",
    );
  expect(egress.length).toBeGreaterThan(0);
  for (const [, init] of egress)
    expect(init?.headers).toMatchObject({
      "X-MindRoom-Connections-User": "@bob:example.org",
    });
});

// The server serves the egress page for its own path segment only, so a
// longer segment gets the portal.
it.each(["/connections", "/connections/", "/connections/egressx"])(
  "renders the connections portal on %s",
  async (pathname) => {
    await loadEntry(pathname);
    expect(
      await screen.findByRole("heading", { name: "Your MindRoom connections" }),
    ).toBeInTheDocument();
  },
);
