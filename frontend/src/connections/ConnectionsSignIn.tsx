import { useEffect, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { agentLinkToOpen, openAgentLink, openChatSignIn } from "./navigation";
import { setSignedInUser } from "./request";

const SESSION_PATH = "/api/connections/session";
const READY_MESSAGE = "mindroom:connections-ready";
const OPENID_MESSAGE = "mindroom:connections-openid";
// MindRoom Chat answers with one token request to its homeserver; an opener that is not Chat never answers.
const ANSWER_TIMEOUT_MS = 5000;
const SENT_TO_CHAT = "mindroom-connections-sent-to-chat";
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const NOT_AVAILABLE = "Connections are not available for this account.";
const REJECTED = "MindRoom did not accept the sign-in from MindRoom Chat.";
const RELOAD = "Could not sign in. Reload this page to try again.";

type Gate =
  | { state: "checking" }
  | { state: "ready" }
  | { state: "chat"; url: string }
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
  if (typeof user !== "string" || !user) return blocked(RELOAD);
  setSignedInUser(user);
  return { state: "ready" };
}

/** The web page the server names for signing in, or `null`. */
function chatSignInUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const { protocol } = new URL(value);
    return protocol === "https:" || protocol === "http:" ? value : null;
  } catch {
    return null;
  }
}

/** Whether this tab was just sent to Chat to sign in, forgetting it; without storage there is no loop guard, so yes. */
function backFromChat(): boolean {
  try {
    const sent = window.sessionStorage.getItem(SENT_TO_CHAT) !== null;
    window.sessionStorage.removeItem(SENT_TO_CHAT);
    return sent;
  } catch {
    return true;
  }
}

/** Show the session the server reports, or send a signed-out visitor to the MindRoom Chat page that signs them in. */
async function sessionGate(signal: AbortSignal): Promise<Gate> {
  const response = await fetch(SESSION_PATH, {
    signal,
    credentials: "same-origin",
  });
  // Read after the probe, so a run that is cancelled first (React runs effects twice in development) keeps it.
  const back = backFromChat();
  if (response.status === 403) return blocked(NOT_AVAILABLE);
  if (response.status !== 401)
    return response.ok ? signedIn(response) : blocked(RELOAD);
  const url = chatSignInUrl((await readBody(response)).sign_in_url);
  // Still signed out right after Chat's page: sending the visitor there again would loop.
  return url && !back ? { state: "chat", url } : blocked(OPEN_FROM_CHAT);
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
  // Trusted-upstream deployments, direct visits, and reloads after Chat's tab closed have no handshake to run.
  if (!opener || opener.closed) return sessionGate(signal);
  const answer = await askOpener(opener, signal);
  // An opener that is not MindRoom Chat, such as a page that linked here, never answers.
  if (!answer) return signal.aborted ? blocked(RELOAD) : sessionGate(signal);
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
 * Render a Connections page for the Matrix user signed in to MindRoom Chat.
 *
 * When MindRoom Chat opened the portal and its tab is still open, it ignores any existing session and only renders after
 * Chat's OpenID handshake succeeds. Otherwise, or when the opener does not answer within five seconds, the page shows
 * the session the server reports. A signed-out visitor goes to the MindRoom Chat page the server names, which signs
 * the browser in and comes back; still signed out right after that, the page explains instead.
 * A `?next=` connect or reset link is opened instead once sign-in settles, whether or not it succeeded.
 *
 * @param children - Page content shown only after sign-in succeeds.
 */
export function ConnectionsSignIn({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void signIn(signal)
      .catch(() => blocked(RELOAD))
      .then((next) => {
        if (signal.aborted) return;
        // Chat opens an agent's connect or reset link through the portal, so the link finds this session.
        // A failed sign-in opens it too, as a plain click would have: the link checks its own sign-in.
        const link = agentLinkToOpen(window.location.search);
        if (link) openAgentLink(link);
        else if (next.state === "chat") {
          window.sessionStorage.setItem(SENT_TO_CHAT, "1");
          openChatSignIn(next.url);
        } else setGate(next);
      });
    return () => controller.abort();
  }, []);

  if (gate.state === "ready") return children;
  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl">
        {gate.state === "blocked" ? (
          <Alert>
            <AlertDescription>{gate.message}</AlertDescription>
          </Alert>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">
            Signing in…
          </p>
        )}
      </div>
    </main>
  );
}
