import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsHeader } from "./ConnectionsHeader";
import type { ConnectionsIdentity } from "./types";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const identity = (
  overrides: Partial<ConnectionsIdentity> = {},
): ConnectionsIdentity => ({
  matrix_user_id: "@bob:example.org",
  email: "bob@example.org",
  is_administrator: false,
  egress_enabled: true,
  portal_enabled: true,
  ...overrides,
});

function answerWith(response: () => Promise<Response>) {
  vi.mocked(fetch).mockImplementation(async (input) => {
    if (String(input) === "/api/connections/me") return response();
    throw new Error(`Unexpected request: ${String(input)}`);
  });
}

const renderHeader = (
  current: "agents" | "keys" = "agents",
  howItWorks?: string,
) =>
  render(
    <ConnectionsHeader
      current={current}
      title="Your title"
      howItWorks={howItWorks}
    >
      The intro.
    </ConnectionsHeader>,
  );

beforeEach(() => answerWith(async () => json(identity())));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("connections header", () => {
  it("shows who is signed in and links the two pages", async () => {
    renderHeader();
    expect(
      await screen.findByText("Signed in as @bob:example.org"),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Your title" })).toBeVisible();
    expect(screen.getByText("The intro.")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Connections pages" });
    expect(
      Array.from(nav.querySelectorAll("a")).map((link) => [
        link.textContent,
        link.getAttribute("href"),
      ]),
    ).toEqual([
      ["Agents", "/connections"],
      ["Agent Keys", "/connections/egress"],
    ]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/connections/me",
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it("marks the page that is open", () => {
    renderHeader("keys");
    expect(screen.getByRole("link", { name: "Agent Keys" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getByRole("link", { name: "Agents" })).not.toHaveAttribute(
      "aria-current",
    );
  });

  it("offers the dashboard to administrators only", async () => {
    renderHeader();
    await screen.findByText("Signed in as @bob:example.org");
    expect(screen.queryByRole("link", { name: "Dashboard" })).toBeNull();
    cleanup();

    answerWith(async () =>
      json(
        identity({
          matrix_user_id: "@alice:example.org",
          is_administrator: true,
        }),
      ),
    );
    renderHeader();
    expect(
      await screen.findByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute(
      "href",
      "/",
    );
  });

  it("links Agent Keys only while the egress broker is on", async () => {
    answerWith(async () => json(identity({ egress_enabled: false })));
    renderHeader();
    await screen.findByText("Signed in as @bob:example.org");
    const nav = screen.getByRole("navigation", { name: "Connections pages" });
    expect(
      Array.from(nav.querySelectorAll("a")).map((link) => link.textContent),
    ).toEqual(["Agents"]);
  });

  it("drops the Agents link where only the Agent Keys page is served", async () => {
    answerWith(async () => json(identity({ portal_enabled: false })));
    renderHeader("keys");
    await screen.findByText("Signed in as @bob:example.org");
    const nav = screen.getByRole("navigation", { name: "Connections pages" });
    expect(
      Array.from(nav.querySelectorAll("a")).map((link) => link.textContent),
    ).toEqual(["Agent Keys"]);
  });

  it("keeps the Agents link, and the open page's, when the identity cannot be loaded", async () => {
    answerWith(async () => json({}, 500));
    renderHeader();
    expect(await screen.findByRole("link", { name: "Agents" })).toBeVisible();
    // Only the identity says whether the broker is on, so the failed request
    // settles before the link is checked.
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    await act(async () => {
      await vi.mocked(fetch).mock.results[0].value;
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(screen.queryByRole("link", { name: "Agent Keys" })).toBeNull();
    expect(screen.queryByText(/Signed in as/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    cleanup();

    // The Agent Keys page is only served while the broker is on.
    renderHeader("keys");
    expect(
      await screen.findByRole("link", { name: "Agent Keys" }),
    ).toBeVisible();
  });
});

describe("the notice after a redirect from the dashboard", () => {
  const NOTICE =
    "The dashboard is for administrators. Manage your agents' keys and accounts here.";
  afterEach(() => {
    window.history.replaceState({}, "", "/");
    window.sessionStorage.clear();
  });

  it("tells someone sent here from the dashboard that it is for administrators, politely", () => {
    window.history.replaceState({}, "", "/connections?notice=admin-only");
    renderHeader();
    // It is told on arrival and is not an error, so a screen reader reads it as a status.
    expect(screen.getByRole("status")).toHaveTextContent(NOTICE);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows nothing without the notice, or for one it does not know", () => {
    renderHeader();
    expect(screen.queryByText(NOTICE)).toBeNull();
    cleanup();
    window.history.replaceState({}, "", "/connections?notice=something-else");
    renderHeader();
    expect(screen.queryByRole("status")).toBeNull();
    cleanup();
    window.history.replaceState({}, "", "/connections?notice=");
    renderHeader();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it.each([
    "__proto__",
    "constructor",
    "toString",
    "hasOwnProperty",
    "valueOf",
    "ADMIN-ONLY",
  ])("never reads %s from an inherited property of the notices", (id) => {
    window.history.replaceState({}, "", `/connections?notice=${id}`);
    renderHeader();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.body).not.toHaveTextContent("[object");
    expect(document.body).not.toHaveTextContent("function");
  });

  it("can be dismissed, and stays dismissed for the browser session", () => {
    window.history.replaceState({}, "", "/connections?notice=admin-only");
    renderHeader();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(NOTICE)).toBeNull();
    cleanup();
    renderHeader();
    expect(screen.queryByText(NOTICE)).toBeNull();
    // A new session has not dismissed it.
    window.sessionStorage.clear();
    cleanup();
    renderHeader();
    expect(screen.getByText(NOTICE)).toBeVisible();
  });

  it("is simply shown when storage is not available", () => {
    window.history.replaceState({}, "", "/connections?notice=admin-only");
    const blocked = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("storage is off");
      });
    const blockedSet = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("storage is off");
      });
    renderHeader();
    expect(screen.getByText(NOTICE)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(NOTICE)).toBeNull();
    blocked.mockRestore();
    blockedSet.mockRestore();
  });
});

describe("the explanation behind How this works", () => {
  it("keeps its chevron still for someone who prefers reduced motion", () => {
    renderHeader("keys", "More.");
    const chevron = screen
      .getByRole("button", { name: "How this works" })
      .querySelector("svg");
    expect(chevron).toHaveClass("transition-transform");
    expect(chevron).toHaveClass("motion-reduce:transition-none");
  });

  it("is hidden until the disclosure is opened, and offered only when there is one", () => {
    renderHeader("keys", "The long explanation.");
    const toggle = screen.getByRole("button", { name: "How this works" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // There is nothing to point at while it is closed.
    expect(toggle).not.toHaveAttribute("aria-controls");
    expect(screen.queryByText("The long explanation.")).toBeNull();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("The long explanation.")).toBeVisible();
    expect(
      document.getElementById(toggle.getAttribute("aria-controls") ?? ""),
    ).toHaveTextContent("The long explanation.");
    cleanup();
    renderHeader();
    expect(screen.queryByRole("button", { name: "How this works" })).toBeNull();
  });
});
