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

it.each(["/connections/egress", "/connections/egress/"])(
  "renders the egress page on %s",
  async (pathname) => {
    await loadEntry(pathname);
    expect(
      await screen.findByRole("heading", { name: "Your agents' API keys" }),
    ).toBeInTheDocument();
  },
);

it.each(["/connections", "/connections/"])(
  "renders the connections portal on %s",
  async (pathname) => {
    await loadEntry(pathname);
    expect(
      await screen.findByRole("heading", { name: "Your MindRoom connections" }),
    ).toBeInTheDocument();
  },
);
