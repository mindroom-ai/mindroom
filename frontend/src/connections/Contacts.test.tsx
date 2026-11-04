import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Contacts } from "./Contacts";
import type { ContactView, ContactsPage } from "./types";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

const agent = {
  agent_name: "mind_bob",
  display_name: "Bob's Mind",
  configured_rules: ["Never share keys."],
  default_tools: ["calculator"],
  judged: true,
  tool_choices: [
    {
      name: "calculator",
      display_name: "Calculator",
      icon: null,
      provider: null,
      executes_code: false,
      reaches_files: false,
    },
    {
      name: "shell",
      display_name: "Shell Commands",
      icon: null,
      provider: null,
      executes_code: true,
      reaches_files: true,
    },
    {
      name: "google_drive",
      display_name: "Google Drive",
      icon: null,
      provider: "google_drive",
      executes_code: false,
      reaches_files: true,
    },
  ],
};

const party = (name: string, owner: string, display: string) => ({
  kind: "agent" as const,
  id: name,
  owner,
  display_name: display,
});
const bob = party("mind_bob", "@bob:example.org", "Bob's Mind");

const contact = (overrides: Partial<ContactView>): ContactView => ({
  id: "c1",
  status: "pending",
  side: "target",
  you: bob,
  other: party("mind_alice", "@alice:example.org", "Alice's Mind"),
  reason: "Plan the dinner",
  room_id: null,
  rules: null,
  tools: null,
  effective_tools: ["calculator", "matrix_message"],
  requested_at: "2026-11-03T09:00:00Z",
  decided_at: null,
  ...overrides,
});

const page = (contacts: ContactView[]): ContactsPage => ({
  agents: [agent],
  contacts,
});

type Handler = (
  path: string,
  init?: RequestInit,
) => Promise<Response> | undefined;

function installApi(contacts: ContactsPage, extra?: Handler) {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const path = String(input);
    const answer = extra?.(path, init);
    if (answer) return answer;
    if (path === "/api/connections/contacts") return json(contacts);
    if (path === "/api/connections/agents/mind_bob/state")
      return json({
        connected: { google_drive: false },
        egress_services: null,
      });
    if (path === "/api/connections/me") return json({}, 500);
    throw new Error(`Unexpected request: ${path}`);
  });
}

const posts = () =>
  vi
    .mocked(fetch)
    .mock.calls.filter(([, init]) => init?.method === "POST")
    .map(([input, init]) => [String(input), JSON.parse(String(init?.body))]);

beforeEach(() => installApi(page([])));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("contacts page", () => {
  it("says how to get a first contact when there is none", async () => {
    render(<Contacts />);
    expect(
      await screen.findByText(
        "No contacts yet. Ask your agent in your personal room to get in touch with someone.",
      ),
    ).toBeVisible();
  });

  it("groups contacts by where they stand", async () => {
    installApi(
      page([
        contact({ id: "sent", side: "requester" }),
        contact({ id: "asked" }),
        contact({ id: "live", status: "active", room_id: "!room:example.org" }),
        contact({ id: "no", status: "declined" }),
      ]),
    );
    render(<Contacts />);
    await screen.findByRole("heading", { name: "Waiting for you" });
    expect(
      screen
        .getAllByRole("heading", { level: 2 })
        .map((heading) => heading.textContent),
    ).toEqual(["Waiting for you", "Active", "Sent", "Declined"]);
    expect(screen.getByRole("link", { name: "Open room" })).toHaveAttribute(
      "href",
      "https://matrix.to/#/!room%3Aexample.org",
    );
  });

  it("shows your side's rules, tools, and judge, and no one else's", async () => {
    installApi(
      page([
        contact({
          status: "active",
          rules: "Share only free or busy.",
          tools: ["calculator", "shell"],
          effective_tools: ["calculator", "shell", "matrix_message"],
        }),
      ]),
    );
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(within(card).getByText("Never share keys.")).toBeVisible();
    expect(within(card).getByText("Share only free or busy.")).toBeVisible();
    expect(within(card).getByText("Shell Commands")).toBeVisible();
    expect(
      within(card).getByText(
        "Runs code: the other side can ask it to reach anything your agent's worker or process can.",
      ),
    ).toBeVisible();
    expect(
      within(card).getByText(
        "A judge checks each message against these rules before it is sent.",
      ),
    ).toBeVisible();
  });

  it("flags a contact tool whose account is not connected", async () => {
    installApi(
      page([
        contact({
          status: "active",
          effective_tools: ["google_drive", "matrix_message"],
        }),
      ]),
    );
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(
      await within(card).findByText(
        "Not connected, so contact turns cannot use it.",
      ),
    ).toBeVisible();
    expect(
      within(card).getByRole("link", { name: "Connect it" }),
    ).toHaveAttribute("href", "/connections");
  });

  it("accepts with the tools chosen in the dialog and shows the new list", async () => {
    const accepted = contact({
      status: "active",
      room_id: "!room:example.org",
    });
    installApi(page([contact({})]), (path, init) =>
      path === "/api/connections/contacts/c1/accept" && init?.method === "POST"
        ? Promise.resolve(json(page([accepted])))
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("checkbox", { name: "Shell Commands" }),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Accept" }));
    expect(
      await screen.findByRole("heading", { name: "Active" }),
    ).toBeVisible();
    expect(posts()).toEqual([
      [
        "/api/connections/contacts/c1/accept",
        { tools: ["calculator", "shell"] },
      ],
    ]);
  });

  it("tells someone asked in person that no agent of theirs takes part", async () => {
    installApi(
      page([
        contact({
          you: {
            kind: "person",
            id: "@bob:example.org",
            owner: "@bob:example.org",
            display_name: "@bob:example.org",
          },
          effective_tools: null,
        }),
      ]),
    );
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(
      within(card).getByText(
        "You talk in the contact room yourself, so no rules or tools apply to your side.",
      ),
    ).toBeVisible();
    fireEvent.click(within(card).getByRole("button", { name: "Accept…" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      "You and Alice's Mind get a shared room to talk in.",
    );
    expect(dialog).not.toHaveTextContent("turns of your agent");
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
  });

  it("saves only the rules when only the rules changed", async () => {
    installApi(page([contact({ status: "active" })]), (path, init) =>
      path === "/api/connections/contacts/c1/update" && init?.method === "POST"
        ? Promise.resolve(json(page([contact({ status: "active" })])))
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Your rules"), {
      target: { value: "Be brief." },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(posts()).toEqual([
        ["/api/connections/contacts/c1/update", { rules: "Be brief." }],
      ]),
    );
  });

  it("says why a change was refused", async () => {
    installApi(page([contact({ status: "active" })]), (path, init) =>
      path === "/api/connections/contacts/c1/remove" && init?.method === "POST"
        ? Promise.resolve(
            json({ detail: "mind_alice is not running, so try again" }, 409),
          )
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "mind_alice is not running, so try again",
    );
  });
});
