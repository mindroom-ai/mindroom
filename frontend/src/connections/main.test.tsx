import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  screen,
  within,
} from "@testing-library/react";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

// main.tsx renders into its own root, which Testing Library's cleanup does not know about.
const roots: Root[] = [];

const json = (value: unknown) => new Response(JSON.stringify(value));

const agent = {
  agent_name: "mind",
  agent_display_name: "Mind",
  is_shared: false,
  can_use: true,
  managers: [],
  mcp_available: false,
  services: [],
  tools: [],
};
const egressAgent = {
  agent_name: "mind",
  agent_display_name: "Mind",
  shared: false,
  scope: "user",
  scope_agents: [],
  managers: [],
  can_manage: true,
  services: [],
  inactive_services: [],
};

/** The Chat tab that opened the portal, answering its ready message with a token as Chat does. */
function openFromChat() {
  const opener = {
    postMessage: vi.fn(() =>
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://chat.example.org",
          source: opener as unknown as MessageEventSource,
          data: {
            type: "mindroom:connections-openid",
            openid_token: { access_token: "bob" },
          },
        }),
      ),
    ),
  };
  Object.defineProperty(window, "opener", {
    configurable: true,
    value: opener,
  });
}

const handshakes = () =>
  vi
    .mocked(fetch)
    .mock.calls.filter(
      ([input, init]) =>
        String(input) === "/api/connections/session" && init?.method === "POST",
    ).length;

async function loadEntry(pathname: string) {
  // An explicit theme keeps ThemeProvider from needing matchMedia in jsdom.
  localStorage.setItem("theme", "light");
  document.body.innerHTML = '<div id="root"></div>';
  window.history.pushState({}, "", pathname);
  vi.resetModules();
  const client = await import("react-dom/client");
  const createRoot = client.default.createRoot;
  vi.spyOn(client.default, "createRoot").mockImplementation((...args) => {
    const root = createRoot(...args);
    roots.push(root);
    return root;
  });
  await act(async () => {
    await import("./main");
  });
}

beforeEach(() => {
  vi.mocked(fetch).mockImplementation(async (input) => {
    const path = String(input);
    if (path === "/api/connections/egress")
      return json({ agents: [egressAgent] });
    if (path === "/api/connections") return json({ agents: [agent] });
    if (path === "/api/connections/agents/mind/state")
      return json({ connected: {}, egress_services: [] });
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
  act(() => roots.splice(0).forEach((root) => root.unmount()));
  Object.defineProperty(window, "opener", { configurable: true, value: null });
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

it("switches tabs without signing in to Chat again, and back follows them", async () => {
  openFromChat();
  await loadEntry("/connections");
  await screen.findByRole("heading", { name: "Your MindRoom connections" });
  await screen.findByText("Signed in as Bob Builder");
  const signedIn = handshakes();
  expect(signedIn).toBeGreaterThan(0);
  const tab = (name: string) =>
    within(
      screen.getByRole("navigation", { name: "Connections pages" }),
    ).getByRole("link", { name });

  fireEvent.click(tab("Agent Keys"));
  expect(window.location.pathname).toBe("/connections/egress");
  expect(screen.getByRole("heading", { name: "Agent Keys" })).toBeVisible();
  // The header keeps the account it already showed instead of blinking out.
  expect(screen.getByText("Signed in as Bob Builder")).toBeVisible();

  fireEvent.click(tab("Agents"));
  expect(window.location.pathname).toBe("/connections");
  expect(tab("Agent Keys")).toBeVisible();

  await act(async () => {
    window.history.back();
    await new Promise((resolve) =>
      window.addEventListener("popstate", resolve, { once: true }),
    );
  });
  expect(window.location.pathname).toBe("/connections/egress");
  expect(screen.getByRole("heading", { name: "Agent Keys" })).toBeVisible();
  expect(handshakes()).toBe(signedIn);
});

it("opens the agent Manage services links to on the Agent Keys page", async () => {
  openFromChat();
  await loadEntry("/connections");
  const signedIn = handshakes();
  fireEvent.click(await screen.findByRole("button", { name: "Expand Mind" }));
  fireEvent.click(await screen.findByRole("link", { name: "Manage services" }));
  expect(window.location.pathname).toBe("/connections/egress");
  expect(window.location.hash).toBe("#egress-agent-mind");
  expect(
    await screen.findByRole("button", { name: "Collapse Mind" }),
  ).toHaveAttribute("aria-expanded", "true");
  expect(handshakes()).toBe(signedIn);
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

it("adds no history entry for the page already open", async () => {
  await loadEntry("/connections?notice=admin-only");
  await screen.findByRole("heading", { name: "Your MindRoom connections" });
  const entries = window.history.length;
  fireEvent.click(await screen.findByRole("link", { name: "Agents" }));
  expect(window.history.length).toBe(entries);
});
