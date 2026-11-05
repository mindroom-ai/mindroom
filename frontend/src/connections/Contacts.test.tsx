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
import type { ContactAgent, ContactView, ContactsPage } from "./types";

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });

const agent: ContactAgent = {
  agent_name: "mind_bob",
  configured_rules: ["Never share keys."],
  judged: true,
  tool_choices: [
    {
      name: "calculator",
      display_name: "Calculator",
      provider: null,
      shared_account: false,
      reach: "none",
    },
    {
      name: "shell",
      display_name: "Shell Commands",
      provider: null,
      shared_account: false,
      reach: "code",
    },
    {
      name: "duckdb",
      display_name: "DuckDB",
      provider: null,
      shared_account: false,
      reach: "unconfined",
    },
    {
      name: "google_drive",
      display_name: "Google Drive",
      provider: "google_drive",
      shared_account: false,
      reach: "files",
    },
    {
      name: "google_calendar",
      display_name: "Google Calendar",
      provider: null,
      shared_account: true,
      reach: "none",
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

function installApi(
  contacts: ContactsPage | (() => ContactsPage),
  extra?: Handler,
) {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const path = String(input);
    const answer = extra?.(path, init);
    if (answer) return answer;
    if (path === "/api/connections/contacts")
      return json(typeof contacts === "function" ? contacts() : contacts);
    if (path === "/api/connections/agents/mind_bob/state")
      return json({
        connected: { google_drive: false },
        egress_services: null,
      });
    if (path === "/api/connections/me") return json({}, 500);
    throw new Error(`Unexpected request: ${path}`);
  });
}

const requests = (path: string) =>
  vi.mocked(fetch).mock.calls.filter(([input]) => String(input) === path);

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
          rules: "- Share only free or busy.",
          effective_tools: ["calculator", "shell", "duckdb", "matrix_message"],
        }),
      ]),
    );
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(within(card).getByText("Never share keys.")).toBeVisible();
    expect(within(card).getByText("- Share only free or busy.")).toBeVisible();
    expect(within(card).getByText("Shell Commands")).toBeVisible();
    expect(
      within(card).getByText(
        "Runs code: the other side can ask it to reach anything your agent's worker or process can.",
      ),
    ).toBeVisible();
    expect(
      within(card).getByText(
        "Not limited to your agent's files: the other side can ask it to read or change anything your agent's worker or process can.",
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
    expect(dialog).toHaveTextContent(
      "Bob's Mind and Alice's Mind get a shared room to talk in.",
    );
    const shell = within(dialog).getByRole("checkbox", {
      name: "Shell Commands",
    });
    expect(shell).toHaveAccessibleDescription(
      "Runs code: the other side can ask it to reach anything your agent's worker or process can.",
    );
    const drive = within(dialog).getByRole("checkbox", {
      name: "Google Drive",
    });
    for (const note of [
      "Reaches your agent's files: the other side can ask for what it can read and change what it can write, such as MEMORY.md.",
      "Acts with your connected account.",
      "Not connected, so contact turns cannot use it. Connect it on the Agents page.",
    ])
      expect(drive).toHaveAccessibleDescription(expect.stringContaining(note));
    expect(
      within(dialog).getByRole("checkbox", { name: "Google Calendar" }),
    ).toHaveAccessibleDescription("Acts with a shared service account.");
    // Leaving the page from the dialog would lose the choices made in it.
    expect(within(dialog).queryByRole("link")).toBeNull();
    fireEvent.click(shell);
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

  it("says that removing keeps the room only for an owner who joined it", async () => {
    installApi(page([contact({ status: "active" })]));
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove…" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "The agents in this contact leave its room. If you joined it, you keep the room and its history; otherwise your invitation is withdrawn.",
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

  it("saves only the tools when a tool is taken away", async () => {
    const active = contact({
      status: "active",
      effective_tools: ["calculator", "shell", "matrix_message"],
    });
    installApi(page([active]), (path, init) =>
      path === "/api/connections/contacts/c1/update" && init?.method === "POST"
        ? Promise.resolve(json(page([active])))
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(
      within(dialog).getByRole("checkbox", { name: "Shell Commands" }),
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(posts()).toEqual([
        ["/api/connections/contacts/c1/update", { tools: ["calculator"] }],
      ]),
    );
  });

  it("sends nothing when nothing was changed", async () => {
    installApi(page([contact({ status: "active" })]));
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Save",
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(posts()).toEqual([]);
  });

  it("declines a request, saying neither side can ask again", async () => {
    installApi(page([contact({})]), (path, init) =>
      path === "/api/connections/contacts/c1/decline" && init?.method === "POST"
        ? Promise.resolve(json(page([contact({ status: "declined" })])))
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Decline…" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(
      "Neither of you can ask again until you remove the declined request.",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "Decline" }));
    expect(
      await screen.findByText(
        "You declined. Remove it to let either of you ask again.",
      ),
    ).toBeVisible();
    expect(posts()).toEqual([["/api/connections/contacts/c1/decline", {}]]);
  });

  it("withdraws a request you sent", async () => {
    installApi(page([contact({ side: "requester" })]), (path, init) =>
      path === "/api/connections/contacts/c1/remove" && init?.method === "POST"
        ? Promise.resolve(json(page([])))
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Withdraw…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Withdraw",
      }),
    );
    await waitFor(() =>
      expect(posts()).toEqual([["/api/connections/contacts/c1/remove", {}]]),
    );
  });

  it("offers only declining a request to an agent whose contacts are off", async () => {
    installApi({
      agents: [],
      contacts: [contact({ effective_tools: null })],
    });
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(
      within(card).getByText(
        "Contacts are off for Bob's Mind, so this request cannot be accepted until they are on again; you can still decline it.",
      ),
    ).toBeVisible();
    expect(within(card).queryByRole("button", { name: "Accept…" })).toBeNull();
    expect(
      within(card).getByRole("button", { name: "Decline…" }),
    ).toBeVisible();
  });

  it("says what it is doing while a change runs", async () => {
    let finish: (response: Response) => void = () => undefined;
    installApi(page([contact({})]), (path, init) =>
      path === "/api/connections/contacts/c1/accept" && init?.method === "POST"
        ? new Promise<Response>((resolve) => {
            finish = resolve;
          })
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Accept",
      }),
    );
    const card = screen.getByRole("article", { name: "Alice's Mind" });
    expect(await within(card).findByRole("status")).toHaveTextContent(
      "Accepting…",
    );
    expect(
      within(card).getByRole("button", { name: "Decline…" }),
    ).toBeDisabled();
    finish(
      json(
        page([
          contact({ status: "active", decided_at: "2026-11-04T10:00:00Z" }),
        ]),
      ),
    );
    expect(await screen.findByText(/Active since/)).toBeVisible();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("reloads the contacts after a refused change", async () => {
    let refused = false;
    let finishReload: (response: Response) => void = () => undefined;
    installApi(page([contact({})]), (path, init) => {
      if (path === "/api/connections/contacts/c1/accept") {
        refused = true;
        return Promise.resolve(
          json({ detail: "Only a pending contact can be accepted" }, 409),
        );
      }
      if (
        path === "/api/connections/contacts" &&
        refused &&
        init?.method === "GET"
      )
        return new Promise<Response>((resolve) => {
          finishReload = resolve;
        });
      return undefined;
    });
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Accept",
      }),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Only a pending contact can be accepted");
    await waitFor(() => expect(alert).toHaveFocus());
    // The reload is still running: the card stays locked but no longer says it is accepting.
    expect(screen.queryByText("Accepting…")).toBeNull();
    expect(screen.getByRole("button", { name: "Decline…" })).toBeDisabled();
    finishReload(json(page([])));
    expect(
      await screen.findByText(
        "No contacts yet. Ask your agent in your personal room to get in touch with someone.",
      ),
    ).toBeVisible();
  });

  it("says when the contacts cannot be loaded", async () => {
    installApi(page([]), (path) =>
      path === "/api/connections/contacts"
        ? Promise.resolve(json({}, 500))
        : undefined,
    );
    render(<Contacts />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not complete the request. Try again.",
    );
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("loads each agent's accounts once, not again after every change", async () => {
    installApi(page([contact({ status: "active" })]), (path, init) =>
      path === "/api/connections/contacts/c1/update" && init?.method === "POST"
        ? Promise.resolve(
            json(page([contact({ status: "active", rules: "x" })])),
          )
        : undefined,
    );
    render(<Contacts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText("Your rules"), {
      target: { value: "x" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("x")).toBeVisible();
    // Let the effects of the new page run before counting.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(requests("/api/connections/agents/mind_bob/state")).toHaveLength(1);
  });

  it("dates a declined contact by its decision", async () => {
    installApi(
      page([
        contact({ status: "declined", decided_at: "2026-11-04T10:00:00Z" }),
      ]),
    );
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(
      [...card.querySelectorAll("time")].map((time) => time.dateTime),
    ).toEqual(["2026-11-03T09:00:00.000Z", "2026-11-04T10:00:00.000Z"]);
    expect(card).toHaveTextContent(/Declined \d/);
    expect(card).not.toHaveTextContent(/Active since/);
  });

  it("offers the asker of a declined request nothing to remove", async () => {
    installApi(page([contact({ status: "declined", side: "requester" })]));
    render(<Contacts />);
    const card = await screen.findByRole("article", { name: "Alice's Mind" });
    expect(
      within(card).getByText(
        "@alice:example.org declined. They can remove it to let you ask again.",
      ),
    ).toBeVisible();
    expect(within(card).queryByRole("button")).toBeNull();
  });
});
