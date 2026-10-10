import { useEffect, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";

const SESSION_PATH = "/api/connections/session";
const READY_MESSAGE = "mindroom:connections-ready";
const OPENID_MESSAGE = "mindroom:connections-openid";
const ANSWER_TIMEOUT_MS = 30000;
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const NOT_AVAILABLE = "Connections are not available for this account.";
const REJECTED = "MindRoom did not accept the sign-in from MindRoom Chat.";
const RELOAD = "Could not sign in. Reload this page to try again.";

type Gate =
  | { state: "checking" }
  | { state: "signed-in"; user: string }
  | { state: "blocked"; message: string };

const blocked = (message: string): Gate => ({ state: "blocked", message });

async function readBody(response: Response): Promise<Record<string, unknown>> {
  try {
    const body: unknown = await response.json();
    return typeof body === "object" && body !== null
      ? (body as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

async function signedIn(response: Response): Promise<Gate> {
  const { matrix_user_id: user } = await readBody(response);
  return typeof user === "string" && user
    ? { state: "signed-in", user }
    : blocked(RELOAD);
}

/** Ask the window that opened the portal for a Matrix OpenID token; `null` means no answer in time or aborted. */
function askOpener(opener: Window, signal: AbortSignal) {
  type Answer = { token: object; origin: string };
  return new Promise<Answer | null>((resolve) => {
    const finish = (answer: Answer | null) => {
      clearTimeout(timer);
      window.removeEventListener("message", onMessage);
      resolve(answer);
    };
    const onMessage = (event: MessageEvent) => {
      const { type, openid_token: token } = (event.data ?? {}) as {
        type?: unknown;
        openid_token?: unknown;
      };
      if (event.source !== opener || type !== OPENID_MESSAGE) return;
      if (typeof token !== "object" || token === null) return;
      finish({ token, origin: event.origin });
    };
    const timer = setTimeout(() => finish(null), ANSWER_TIMEOUT_MS);
    signal.addEventListener("abort", () => finish(null), { once: true });
    window.addEventListener("message", onMessage);
    // The ready message carries no secret; the backend checks the reply's origin against its allowlist.
    opener.postMessage({ type: READY_MESSAGE }, "*");
  });
}

async function signIn(signal: AbortSignal): Promise<Gate> {
  const opener: Window | null = window.opener;
  if (!opener) {
    // Trusted-upstream deployments and direct visits with a live session have no handshake to run.
    const response = await fetch(SESSION_PATH, {
      signal,
      credentials: "same-origin",
    });
    if (response.status === 401) return blocked(OPEN_FROM_CHAT);
    if (response.status === 403) return blocked(NOT_AVAILABLE);
    return response.ok ? signedIn(response) : blocked(RELOAD);
  }
  const answer = await askOpener(opener, signal);
  if (!answer) return blocked(OPEN_FROM_CHAT);
  const response = await fetch(SESSION_PATH, {
    method: "POST",
    signal,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      openid_token: answer.token,
      client_origin: answer.origin,
    }),
  });
  if (response.ok) return signedIn(response);
  if (response.status !== 401 && response.status !== 403)
    return blocked(RELOAD);
  const { detail } = await readBody(response);
  return blocked(typeof detail === "string" && detail ? detail : REJECTED);
}

/**
 * Render the portal for the Matrix user signed in to MindRoom Chat.
 *
 * When MindRoom Chat opened the portal, its OpenID handshake is the only way in and the portal ignores any existing
 * session, so another account's session can never show. Without an opener, the portal shows the session the server reports.
 *
 * @param children - Portal content shown only after sign-in succeeds.
 */
export function ConnectionsSignIn({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void signIn(signal)
      .catch(() => blocked(RELOAD))
      .then((next) => {
        if (!signal.aborted) setGate(next);
      });
    return () => controller.abort();
  }, []);

  if (gate.state === "signed-in")
    return (
      <>
        <div className="bg-muted/20 px-4 pt-4 sm:px-6">
          <p className="mx-auto max-w-6xl text-sm text-muted-foreground">
            Signed in as {gate.user}
          </p>
        </div>
        {children}
      </>
    );
  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl">
        {gate.state === "checking" ? (
          <p role="status" className="text-sm text-muted-foreground">
            Signing in…
          </p>
        ) : (
          <Alert>
            <AlertDescription>{gate.message}</AlertDescription>
          </Alert>
        )}
      </div>
    </main>
  );
}
