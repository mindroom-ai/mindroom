import { StrictMode, useEffect } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsSignIn } from "./ConnectionsSignIn";

const SESSION = "/api/connections/session";
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const TRY_AGAIN = "Could not sign in. Try again.";
const chatOrigin = "https://chat.example.org";
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });
const tokenFor = (name: string) => ({
  access_token: name,
  expires_in: 3600,
});

let opener: { postMessage: ReturnType<typeof vi.fn> } | null;
const mounts = vi.fn();
const unmounts = vi.fn();

// Fake backend: GET reports the session user, POST signs in the token's user.
// Queued POST outcomes (a response or a thrown error) are used first.
// Like a real fetch, requests whose signal aborts mid-flight are rejected.
function installBackend({
  session = null,
  anonymous = 401,
  postCreatesSession = true,
  postOutcomes = [],
}: {
  session?: string | null;
  anonymous?: number;
  postCreatesSession?: boolean;
  postOutcomes?: (Response | Error)[];
} = {}) {
  let user = session ? `@${session}:example.org` : null;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) !== SESSION)
      throw new Error(`Unexpected request: ${String(input)}`);
    await Promise.resolve();
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (init?.method === "POST") {
      const outcome = postOutcomes.shift();
      if (outcome instanceof Error) throw outcome;
      if (outcome) return outcome;
      const body = JSON.parse(init.body as string) as {
        openid_token: { access_token: string };
      };
      const signedIn = `@${body.openid_token.access_token}:example.org`;
      if (postCreatesSession) user = signedIn;
      return json({ matrix_user_id: signedIn });
    }
    return user ? json({ matrix_user_id: user }) : json({}, anonymous);
  });
}

function PortalContent() {
  useEffect(() => {
    mounts();
    return () => unmounts();
  }, []);
  return <p>Portal content</p>;
}

function renderGate() {
  return render(
    <StrictMode>
      <ConnectionsSignIn>
        <PortalContent />
      </ConnectionsSignIn>
    </StrictMode>,
  );
}

function answerFromOpener(name = "alice") {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: chatOrigin,
        source: opener as unknown as MessageEventSource,
        data: {
          type: "mindroom:connections-openid",
          openid_token: tokenFor(name),
        },
      }),
    );
  });
}

const sessionCalls = (method: string) =>
  vi
    .mocked(fetch)
    .mock.calls.filter(
      ([input, init]) =>
        String(input) === SESSION && (init?.method ?? "GET") === method,
    );

function removeOpener() {
  Object.defineProperty(window, "opener", { configurable: true, value: null });
}

// One portal instance is live when every mount but the current one has unmounted.
const liveInstances = () =>
  mounts.mock.calls.length - unmounts.mock.calls.length;

beforeEach(() => {
  mounts.mockClear();
  unmounts.mockClear();
  opener = { postMessage: vi.fn() };
  Object.defineProperty(window, "opener", {
    configurable: true,
    value: opener,
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  removeOpener();
});

describe("ConnectionsSignIn", () => {
  it("renders an existing session without asking for a token when there is no opener", async () => {
    removeOpener();
    installBackend({ session: "alice" });
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

  it("keeps rendering the same user once the opener confirms the session", async () => {
    installBackend({ session: "alice" });
    renderGate();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalledOnce());
    const mounted = mounts.mock.calls.length;
    const probes = sessionCalls("GET").length;
    answerFromOpener("alice");
    await waitFor(() => expect(sessionCalls("GET")).toHaveLength(probes + 1));
    await act(() => Promise.resolve());
    expect(
      screen.getByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    expect(mounts).toHaveBeenCalledTimes(mounted);
    expect(liveInstances()).toBe(1);
    expect(sessionCalls("POST")).toHaveLength(1);
  });

  it("replaces a stale session with the account signed in to Chat and remounts the portal", async () => {
    installBackend({ session: "alice" });
    renderGate();
    expect(
      await screen.findByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalledOnce());
    const mountedAsAlice = mounts.mock.calls.length;
    answerFromOpener("bob");
    expect(
      await screen.findByText("Signed in as @bob:example.org"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/@alice:example\.org/)).not.toBeInTheDocument();
    expect(mounts.mock.calls.length).toBeGreaterThan(mountedAsAlice);
    expect(liveInstances()).toBe(1);
    expect(JSON.parse(sessionCalls("POST")[0][1]!.body as string)).toEqual({
      openid_token: tokenFor("bob"),
      client_origin: chatOrigin,
    });
  });

  it("keeps an existing session when the opener does not answer", async () => {
    vi.useFakeTimers();
    installBackend({ session: "alice" });
    renderGate();
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(opener!.postMessage).toHaveBeenCalledOnce();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(
      screen.getByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    expect(screen.getByText("Portal content")).toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it("hides an existing session when the server rejects the Chat account", async () => {
    installBackend({
      session: "alice",
      postOutcomes: [
        json({ detail: "Matrix OpenID verification failed." }, 401),
      ],
    });
    renderGate();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalledOnce());
    answerFromOpener("bob");
    expect(
      await screen.findByText("Matrix OpenID verification failed."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(liveInstances()).toBe(0);
  });

  it("signs in through the opener after a 401 then renders children", async () => {
    installBackend();
    renderGate();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    answerFromOpener();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    expect(
      screen.getByText("Signed in as @alice:example.org"),
    ).toBeInTheDocument();
    // StrictMode double-runs the effect, yet the opener is asked only once.
    expect(opener!.postMessage).toHaveBeenCalledExactlyOnceWith(
      { type: "mindroom:connections-ready" },
      "*",
    );
    const posts = sessionCalls("POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0][1]!.body as string)).toEqual({
      openid_token: tokenFor("alice"),
      client_origin: chatOrigin,
    });
  });

  it("shows open-from-chat message when the opener does not answer", async () => {
    vi.useFakeTimers();
    installBackend();
    renderGate();
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(opener!.postMessage).toHaveBeenCalledOnce();
    expect(screen.queryByText(OPEN_FROM_CHAT)).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(screen.getByText(OPEN_FROM_CHAT)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it("shows the message without waiting when there is no opener", async () => {
    removeOpener();
    installBackend();
    renderGate();
    expect(await screen.findByText(OPEN_FROM_CHAT)).toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it("does not sign in a second time on its own when the session is still missing", async () => {
    installBackend({ postCreatesSession: false });
    renderGate();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
    answerFromOpener();
    expect(await screen.findByText(TRY_AGAIN)).toBeInTheDocument();
    expect(opener!.postMessage).toHaveBeenCalledOnce();
    expect(sessionCalls("POST")).toHaveLength(1);
  });

  it.each([
    [401, "OpenID server does not match the configured Matrix server."],
    [403, "Connections sign-in is not allowed from this client"],
  ])(
    "shows the server detail when sign-in fails with %i",
    async (status, detail) => {
      installBackend({ postOutcomes: [json({ detail }, status)] });
      renderGate();
      await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
      answerFromOpener();
      expect(await screen.findByText(detail)).toBeInTheDocument();
      expect(screen.queryByText(OPEN_FROM_CHAT)).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Try again" }),
      ).not.toBeInTheDocument();
    },
  );

  it("offers a retry after a server error and signs in when retried", async () => {
    installBackend({
      postOutcomes: [
        json({ detail: "Matrix OpenID verifier is unavailable." }, 503),
      ],
    });
    renderGate();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
    answerFromOpener();
    expect(await screen.findByText(TRY_AGAIN)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalledTimes(2));
    answerFromOpener();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(2);
  });

  it("offers a retry when the sign-in request cannot reach the server", async () => {
    installBackend({ postOutcomes: [new TypeError("Failed to fetch")] });
    renderGate();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
    answerFromOpener();
    expect(await screen.findByText(TRY_AGAIN)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();
  });

  it.each([
    [403, "Connections are not available for this account.", false],
    [404, "Connections are not enabled on this server.", false],
    [503, TRY_AGAIN, true],
  ])(
    "answers a %i session probe without asking the opener",
    async (status, message, retry) => {
      installBackend({ anonymous: status });
      renderGate();
      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(Boolean(screen.queryByRole("button", { name: "Try again" }))).toBe(
        retry,
      );
      expect(opener!.postMessage).not.toHaveBeenCalled();
      expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    },
  );
});
