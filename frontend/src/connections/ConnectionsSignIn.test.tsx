import { StrictMode } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectionsSignIn } from "./ConnectionsSignIn";

const SESSION = "/api/connections/session";
const openidToken = { access_token: "syt_token", expires_in: 3600 };
const chatOrigin = "https://chat.example.org";
const json = (value: unknown, code = 200) =>
  new Response(JSON.stringify(value), { status: code });
const alice = () => json({ matrix_user_id: "@alice:example.org" });

let opener: { postMessage: ReturnType<typeof vi.fn> } | null;

// Fake backend: GET reports the session, POST creates it unless rejected.
// Like a real fetch, requests whose signal aborts mid-flight are rejected.
function installBackend({
  signedIn = false,
  anonymous = 401,
  postCreatesSession = true,
}: {
  signedIn?: boolean;
  anonymous?: number;
  postCreatesSession?: boolean;
} = {}) {
  let hasSession = signedIn;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) !== SESSION)
      throw new Error(`Unexpected request: ${String(input)}`);
    await Promise.resolve();
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (init?.method === "POST") {
      hasSession ||= postCreatesSession;
      return json({});
    }
    return hasSession ? alice() : json({}, anonymous);
  });
}

function renderGate() {
  return render(
    <StrictMode>
      <ConnectionsSignIn>
        <p>Portal content</p>
      </ConnectionsSignIn>
    </StrictMode>,
  );
}

function answerFromOpener() {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        origin: chatOrigin,
        source: opener as unknown as MessageEventSource,
        data: {
          type: "mindroom:connections-openid",
          openid_token: openidToken,
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

beforeEach(() => {
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
  Object.defineProperty(window, "opener", { configurable: true, value: null });
});

describe("ConnectionsSignIn", () => {
  it("renders children and the signed-in user when the session exists", async () => {
    installBackend({ signedIn: true });
    renderGate();
    expect(await screen.findByText("Portal content")).toBeInTheDocument();
    expect(screen.getByText("Signed in as @alice:example.org")).toHaveClass(
      "text-muted-foreground",
    );
    expect(opener!.postMessage).not.toHaveBeenCalled();
    expect(sessionCalls("POST")).toHaveLength(0);
    expect(fetch).toHaveBeenCalledWith(
      SESSION,
      expect.objectContaining({ credentials: "same-origin" }),
    );
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
      openid_token: openidToken,
      client_origin: chatOrigin,
    });
  });

  it("shows open-from-chat message when the opener does not answer", async () => {
    vi.useFakeTimers();
    installBackend();
    renderGate();
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(opener!.postMessage).toHaveBeenCalledOnce();
    expect(
      screen.queryByText("Open Connections from MindRoom Chat to sign in."),
    ).not.toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(
      screen.getByText("Open Connections from MindRoom Chat to sign in."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it("shows the message without waiting when there is no opener", async () => {
    Object.defineProperty(window, "opener", {
      configurable: true,
      value: null,
    });
    installBackend();
    renderGate();
    expect(
      await screen.findByText(
        "Open Connections from MindRoom Chat to sign in.",
      ),
    ).toBeInTheDocument();
    expect(sessionCalls("POST")).toHaveLength(0);
  });

  it("does not sign in a second time when the session is still missing", async () => {
    installBackend({ postCreatesSession: false });
    renderGate();
    await waitFor(() => expect(opener!.postMessage).toHaveBeenCalled());
    answerFromOpener();
    expect(
      await screen.findByText(
        "Open Connections from MindRoom Chat to sign in.",
      ),
    ).toBeInTheDocument();
    expect(opener!.postMessage).toHaveBeenCalledOnce();
    expect(sessionCalls("POST")).toHaveLength(1);
  });

  it("explains accounts that are not allowed to use Connections", async () => {
    installBackend({ anonymous: 403 });
    renderGate();
    expect(
      await screen.findByText(
        "Connections are not available for this account.",
      ),
    ).toBeInTheDocument();
    expect(opener!.postMessage).not.toHaveBeenCalled();
    expect(screen.queryByText("Portal content")).not.toBeInTheDocument();
  });
});
