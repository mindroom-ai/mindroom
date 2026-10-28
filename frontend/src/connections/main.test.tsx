import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  screen,
  within,
} from "@testing-library/react";
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
        display_name: "Bob Builder",
        email: null,
        is_administrator: false,
        egress_enabled: true,
        dashboard_url: "/",
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

it("switches tabs without signing in again, and back follows them", async () => {
  await loadEntry("/connections");
  await screen.findByRole("heading", { name: "Your MindRoom connections" });
  const tab = (name: string) =>
    within(
      screen.getByRole("navigation", { name: "Connections pages" }),
    ).getByRole("link", { name });
  await screen.findByText("Signed in as Bob Builder");
  const signIns = () =>
    vi
      .mocked(fetch)
      .mock.calls.filter(
        ([input]) => String(input) === "/api/connections/session",
      ).length;
  const signedIn = signIns();

  fireEvent.click(tab("Agent Keys"));
  expect(window.location.pathname).toBe("/connections/egress");
  expect(screen.getByRole("heading", { name: "Agent Keys" })).toBeVisible();
  // The header keeps the account it already showed instead of blinking out.
  expect(screen.getByText("Signed in as Bob Builder")).toBeVisible();

  await act(async () => {
    window.history.back();
    await new Promise((resolve) =>
      window.addEventListener("popstate", resolve, { once: true }),
    );
  });
  expect(window.location.pathname).toBe("/connections");
  expect(
    screen.getByRole("heading", { name: "Your MindRoom connections" }),
  ).toBeVisible();
  expect(signIns()).toBe(signedIn);
});

it("leaves a modified click on a tab to the browser", async () => {
  await loadEntry("/connections");
  await screen.findByRole("heading", { name: "Your MindRoom connections" });
  const link = await screen.findByRole("link", { name: "Agent Keys" });
  const click = createEvent.click(link, { ctrlKey: true });
  fireEvent(link, click);
  expect(click.defaultPrevented).toBe(false);
  expect(window.location.pathname).toBe("/connections");
});
