import { useEffect, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { agentLinkToOpen, openAgentLink, openChatSignIn } from "./navigation";
import { setSignedInUser } from "./request";

const SESSION_PATH = "/api/connections/session";
const READY_MESSAGE = "mindroom:connections-ready";
const OPENID_MESSAGE = "mindroom:connections-openid";
// MindRoom Chat answers with a few requests to its homeserver; an opener that is not Chat never answers.
const ANSWER_TIMEOUT_MS = 5000;
const SENT_TO_CHAT = "mindroom-connections-sent-to-chat";
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const SIGN_IN_TO_CHAT = "Sign in to MindRoom Chat to use Connections.";
const NOT_AVAILABLE = "Connections are not available for this account.";
const REJECTED = "MindRoom did not accept the sign-in from MindRoom Chat.";
const RELOAD = "Could not sign in. Reload this page to try again.";

type Answer = { token: object; origin: string };

type Gate =
  | { state: "checking" }
  | { state: "ready" }
  | { state: "chat"; url: string }
  | { state: "blocked"; message: string; chatUrl?: string };

const blocked = (message: string): Gate => ({ state: "blocked", message });
// Shown instead of leaving for Chat's page again; a click cannot loop.
const linkToChat = (url: string): Gate => ({
  state: "blocked",
  message: SIGN_IN_TO_CHAT,
  chatUrl: url,
});

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

/**
 * Show the session the server reports (unless `useSession` is false), or send the visitor to the MindRoom Chat page
 * that signs the browser in.
 */
async function sessionGate(
  signal: AbortSignal,
  useSession: boolean,
): Promise<Gate> {
  const response = await fetch(SESSION_PATH, {
    signal,
    credentials: "same-origin",
  });
  // Read after the probe, so a run that is cancelled first (React runs effects twice in development) keeps it.
  const back = backFromChat();
  if (response.status === 403) return blocked(NOT_AVAILABLE);
  if (useSession && response.ok) return signedIn(response);
  if (!response.ok && response.status !== 401) return blocked(RELOAD);
  const url = chatSignInUrl((await readBody(response)).sign_in_url);
  if (!url) return blocked(OPEN_FROM_CHAT);
  // Still signed out right after Chat's page: sending the visitor there again would loop.
  return back ? linkToChat(url) : { state: "chat", url };
}

/**
 * Ask the window that opened the portal for a Matrix OpenID token; `null` means no answer in time or aborted.
 * The first answer that arrives after the timeout goes to `onLate` instead, until `signal` aborts.
 */
function askOpener(
  opener: Window,
  signal: AbortSignal,
  onLate: (answer: Answer) => void,
) {
  return new Promise<Answer | null>((resolve) => {
    let waiting = true;
    const stop = () => window.removeEventListener("message", onMessage);
    const onMessage = (event: MessageEvent) => {
      const { type, openid_token: token } = (event.data ?? {}) as {
        type?: unknown;
        openid_token?: unknown;
      };
      if (event.source !== opener || type !== OPENID_MESSAGE) return;
      if (typeof token !== "object" || token === null) return;
      stop();
      if (!waiting) return onLate({ token, origin: event.origin });
      clearTimeout(timer);
      resolve({ token, origin: event.origin });
    };
    const timer = setTimeout(() => {
      waiting = false;
      resolve(null);
    }, ANSWER_TIMEOUT_MS);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        stop();
        resolve(null);
      },
      { once: true },
    );
    window.addEventListener("message", onMessage);
    // The ready message carries no secret; the backend checks the reply's origin against its allowlist.
    opener.postMessage({ type: READY_MESSAGE }, "*");
  });
}

function postAnswer(answer: Answer, signal: AbortSignal) {
  return fetch(SESSION_PATH, {
    method: "POST",
    signal,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      openid_token: answer.token,
      client_origin: answer.origin,
    }),
  });
}

/** Sign in Chat's answer that came after the page gave up waiting; `null` when MindRoom refuses it. */
async function signInLateAnswer(
  answer: Answer,
  signal: AbortSignal,
): Promise<Gate | null> {
  const response = await postAnswer(answer, signal);
  return response.ok ? signedIn(response) : null;
}

async function signIn(
  signal: AbortSignal,
  onLateSignIn: (gate: Gate) => void,
): Promise<Gate> {
  const opener: Window | null = window.opener;
  // Trusted-upstream deployments, direct visits, and reloads after Chat's tab closed have no handshake to run.
  if (!opener || opener.closed) return sessionGate(signal, true);
  const answer = await askOpener(opener, signal, (late) => {
    void signInLateAnswer(late, signal)
      .then((gate) => gate && !signal.aborted && onLateSignIn(gate))
      .catch(() => undefined);
  });
  if (signal.aborted) return blocked(RELOAD);
  // An opener that is not MindRoom Chat, such as a page that linked here, never answers, and a silent Chat may be
  // signed in to another account than the browser's session: sign in through Chat's page instead.
  if (!answer) return sessionGate(signal, false);
  const response = await postAnswer(answer, signal);
  if (response.ok) return signedIn(response);
  if (response.status !== 401 && response.status !== 403)
    return blocked(RELOAD);
  const { detail } = await readBody(response);
  return blocked(typeof detail === "string" && detail ? detail : REJECTED);
}

/** Remember that this tab leaves for Chat's sign-in page; `false` when the tab cannot remember it. */
function markSentToChat(): boolean {
  try {
    window.sessionStorage.setItem(SENT_TO_CHAT, "1");
    return true;
  } catch {
    return false;
  }
}

/**
 * Render a Connections page for the Matrix user signed in to MindRoom Chat.
 *
 * When MindRoom Chat opened the portal and its tab is still open, it ignores any existing session and only renders after
 * Chat's OpenID handshake succeeds; when the opener does not answer within five seconds, the page goes to the MindRoom
 * Chat page the server names, which signs the browser in and comes back, and a later answer from Chat still signs in.
 * Otherwise the page shows the session the server reports, and a signed-out visitor goes to that Chat page too.
 * Still signed out right after that page, the visitor gets a link to it instead.
 * A `?next=` connect or reset link is opened instead once sign-in settles, whether or not it succeeded.
 *
 * @param children - Page content shown only after sign-in succeeds.
 */
export function ConnectionsSignIn({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void signIn(signal, setGate)
      .catch(() => blocked(RELOAD))
      .then((next) => {
        if (signal.aborted) return;
        // Chat opens an agent's connect or reset link through the portal, so the link finds this session.
        // A failed sign-in opens it too, as a plain click would have: the link checks its own sign-in.
        const link = agentLinkToOpen(window.location.search);
        if (link) openAgentLink(link);
        else if (next.state !== "chat") setGate(next);
        // Without the mark, a visitor who comes back still signed out would be sent again and again.
        else if (markSentToChat()) openChatSignIn(next.url);
        else setGate(linkToChat(next.url));
      });
    return () => controller.abort();
  }, []);

  if (gate.state === "ready") return children;
  return (
    <main className="min-h-screen bg-muted/20 px-4 py-8 sm:px-6 sm:py-12">
      <div className="mx-auto max-w-6xl">
        {gate.state === "blocked" ? (
          <Alert>
            <AlertDescription>
              {gate.message}
              {gate.chatUrl ? (
                <>
                  {" "}
                  <a className="underline" href={gate.chatUrl}>
                    Open MindRoom Chat
                  </a>
                </>
              ) : null}
            </AlertDescription>
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
