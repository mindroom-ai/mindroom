import { StrictMode } from "react";
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsSignIn } from "./ConnectionsSignIn";

const SESSION = "/api/connections/session";
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const NOT_AVAILABLE = "Connections are not available for this account.";
const REJECTED = "MindRoom did not accept the sign-in from MindRoom Chat.";
const RELOAD = "Could not sign in. Reload this page to try again.";
const chatOrigin = "https://chat.example.org";
const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const tokenFor = (name: string) => ({ access_token: name, expires_in: 3600 });

let opener: { postMessage: ReturnType<typeof vi.fn> } | null;

// Fake backend: GET reports `get`, POST signs in the token's user unless `post` says otherwise.
// Like a real fetch, requests whose signal is aborted are rejected.
function installBackend({
  get = json({}, 401),
  post,
}: { get?: Response | Error; post?: Response | Error } = {}) {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) !== SESSION)
      throw new Error(`Unexpected request: ${String(input)}`);
    await Promise.resolve();
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const isPost = init?.method === "POST";
    const outcome = isPost ? post : get;
    if (outcome instanceof Error) throw outcome;
    if (outcome) return outcome.clone();
    const body = JSON.parse(init!.body as string) as {
      openid_token: { access_token: string };
    };
    return json({
      matrix_user_id: `@${body.openid_token.access_token}:example.org`,
    });
  });
}

const sessionCalls = (method: string) =>
  vi
    .mocked(fetch)
    .mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);

function renderGate() {
  return render(
    <StrictMode>
      <ConnectionsSignIn>
        <p>Portal content</p>
      </ConnectionsSignIn>
    </StrictMode>,
  );
}

function message(
  data: unknown,
  { source = opener as unknown, origin = chatOrigin } = {},
) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin,
        source: source as MessageEventSource | null,
        data,
      }),
    );
  });
}

const answerFromOpener = (name = "alice") =>
  message({
    type: "mindroom:connections-openid",
    openid_token: tokenFor(name),
  });

function setOpener(value: typeof opener) {
  opener = value;
  Object.defineProperty(window, "opener", { configurable: true, value });
}

const messageListeners = (spy: { mock: { calls: unknown[][] } }) =>
  spy.mock.calls.filter(([type]) => type === "message").length;

beforeEach(() => setOpener({ postMessage: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  setOpener(null);
});

describe("ConnectionsSignIn with an opener", () => {
  it("signs in through the opener with one POST and no session probe", async () => {
    installBackend();
    renderGate();
    expect(screen.getByRole("status")).toHaveTextContent("Signing in");
    // StrictMode runs the effect twice: one ready message per run, but only the live run listens.
    expect(opener!.postMessage).toHaveBeenCalledTimes(2);
    expect(opener!.postMessage).toHaveBeenCalledWith(
      { type: "mindroom:connections-ready" },
      "*",
    );
    answerFromOpener();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    expect(screen.getByText("Signed in as @alice:example.org")).toHaveClass(
      "text-muted-foreground",
    );
    expect(sessionCalls("GET")).toHaveLength(0);
    const posts = sessionCalls("POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][1]).toMatchObject({ credentials: "same-origin" });
    expect(JSON.parse(posts[0][1]!.body as string)).toEqual({
      openid_token: tokenFor("alice"),
      client_origin: chatOrigin,
    });
    // A second answer (Chat replies to every ready message) is ignored.
    answerFromOpener("bob");
    await act(() => Promise.resolve());
    expect(sessionCalls("POST")).toHaveLength(1);
    expect(screen.queryByText(/@bob/)).not.toBeInTheDocument();
  });

  it("ignores an existing session and shows only the account Chat signs in", async () => {
    installBackend({ get: json({ matrix_user_id: "@alice:example.org" }) });
    renderGate();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    answerFromOpener("bob");
    expect(
      await screen.findByText("Signed in as @bob:example.org"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/@alice/)).not.toBeInTheDocument();
    expect(sessionCalls("GET")).toHaveLength(0);
  });

  it("shows the open-from-Chat copy and no content when Chat never answers", async () => {
    vi.useFakeTimers();
    installBackend();
    renderGate();
    await act(() => vi.advanceTimersByTimeAsync(29_999));
    expect(screen.queryByText(OPEN_FROM_CHAT)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText(OPEN_FROM_CHAT)).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([
    [401, "OpenID server does not match the configured Matrix server."],
    [403, "Connections sign-in is not allowed from this client"],
  ])(
    "shows the server detail when sign-in fails with %i",
    async (status, detail) => {
      installBackend({ post: json({ detail }, status) });
      renderGate();
      answerFromOpener();
      expect(await screen.findByText(detail)).toBeInTheDocument();
      expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    },
  );

  it.each([
    ["a body that is not JSON", new Response("Forbidden", { status: 403 })],
    ["a detail that is not a string", json({ detail: [{ msg: "x" }] }, 401)],
    ["an empty detail", json({ detail: "" }, 401)],
  ])("falls back to a fixed message for %s", async (_case, post) => {
    installBackend({ post });
    renderGate();
    answerFromOpener();
    expect(await screen.findByText(REJECTED)).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
  });

  it.each([
    ["a 503", json({ detail: "Verifier is unavailable." }, 503)],
    ["a network error", new TypeError("Failed to fetch")],
    ["a success body that is not JSON", new Response("ok", { status: 200 })],
    ["a success body without a user", json({ matrix_user_id: 7 })],
  ])("shows the reload copy after %s", async (_case, post) => {
    installBackend({ post });
    renderGate();
    answerFromOpener();
    expect(await screen.findByText(RELOAD)).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(screen.queryByText(/Verifier/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(opener!.postMessage).toHaveBeenCalledTimes(2);
    expect(sessionCalls("POST")).toHaveLength(1);
  });

  it("accepts only an openid message from the opener with an object token", async () => {
    installBackend();
    renderGate();
    const answer = { type: "mindroom:connections-openid" };
    message({ ...answer, openid_token: tokenFor("mallory") }, { source: {} });
    message({ ...answer, openid_token: tokenFor("mallory") }, { source: null });
    for (const data of [
      null,
      "mindroom:connections-openid",
      { type: "mindroom:connections-ready", openid_token: tokenFor("mallory") },
      answer,
      { ...answer, openid_token: "mallory" },
      { ...answer, openid_token: null },
    ])
      message(data);
    await act(() => Promise.resolve());
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    answerFromOpener("alice");
    expect(
      await screen.findByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(1);
  });

  it("uses the origin of the answering message as the client origin", async () => {
    installBackend();
    renderGate();
    message(
      { type: "mindroom:connections-openid", openid_token: tokenFor("alice") },
      { origin: "https://other-chat.example.org" },
    );
    await screen.findByText("Portal content");
    expect(
      JSON.parse(sessionCalls("POST")[0][1]!.body as string),
    ).toMatchObject({
      client_origin: "https://other-chat.example.org",
    });
  });

  it("leaves no listener, timer, or POST behind when unmounted while waiting", async () => {
    vi.useFakeTimers();
    installBackend();
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    renderGate().unmount();
    expect(messageListeners(add)).toBe(2);
    expect(messageListeners(remove)).toBe(2);
    expect(vi.getTimerCount()).toBe(0);
    answerFromOpener();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("cancels an in-flight POST and ignores its result when unmounted", async () => {
    let posted: AbortSignal | undefined;
    vi.mocked(fetch).mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          posted = init!.signal!;
          posted.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    const errors = vi.spyOn(console, "error");
    const view = renderGate();
    answerFromOpener();
    await act(() => Promise.resolve());
    expect(posted).toBeDefined();
    view.unmount();
    await act(() => Promise.resolve());
    expect(posted!.aborted).toBe(true);
    expect(errors).not.toHaveBeenCalled();
  });
});

describe("ConnectionsSignIn without an opener", () => {
  beforeEach(() => setOpener(null));

  it("renders the session the server reports without asking anyone", async () => {
    installBackend({ get: json({ matrix_user_id: "@alice:example.org" }) });
    renderGate();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    expect(screen.getByText("Signed in as @alice:example.org")).toHaveClass(
      "text-muted-foreground",
    );
    expect(sessionCalls("POST")).toHaveLength(0);
    expect(fetch).toHaveBeenCalledWith(
      SESSION,
      expect.objectContaining({ credentials: "same-origin" }),
    );
  });

  it.each([
    [401, OPEN_FROM_CHAT],
    [403, NOT_AVAILABLE],
    [404, RELOAD],
    [500, RELOAD],
  ])("shows the copy for a %i probe", async (status, copy) => {
    installBackend({ get: json({}, status) });
    renderGate();
    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it.each([
    ["a user that is not a string", json({ matrix_user_id: null })],
    ["a body that is not JSON", new Response("ok", { status: 200 })],
    ["a network error", new TypeError("Failed to fetch")],
  ])("shows the reload copy for %s", async (_case, get) => {
    installBackend({ get });
    renderGate();
    expect(await screen.findByText(RELOAD)).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
  });
});
