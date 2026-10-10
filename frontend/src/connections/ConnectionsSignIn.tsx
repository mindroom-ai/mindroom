import { useEffect, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { signInWithMatrix } from "./matrixSignIn";

const SESSION_PATH = "/api/connections/session";
const OPEN_FROM_CHAT = "Open Connections from MindRoom Chat to sign in.";
const NOT_AVAILABLE = "Connections are not available for this account.";

type Gate =
  | { state: "checking" }
  | { state: "signed-in"; user: string }
  | { state: "blocked"; message: string };

async function probeSession(
  signal: AbortSignal,
): Promise<{ status: number; user: string | null }> {
  const response = await fetch(SESSION_PATH, {
    method: "GET",
    signal,
    credentials: "same-origin",
  });
  if (!response.ok) return { status: response.status, user: null };
  try {
    const body = (await response.json()) as { matrix_user_id?: unknown };
    return {
      status: response.status,
      user:
        typeof body.matrix_user_id === "string" ? body.matrix_user_id : null,
    };
  } catch {
    return { status: response.status, user: null };
  }
}

function resolveProbe(probe: {
  status: number;
  user: string | null;
}): Gate | null {
  if (probe.status === 200 && probe.user)
    return { state: "signed-in", user: probe.user };
  if (probe.status === 403) return { state: "blocked", message: NOT_AVAILABLE };
  if (probe.status === 401) return null;
  return { state: "blocked", message: OPEN_FROM_CHAT };
}

/**
 * Render the portal once the browser holds a Connections session.
 *
 * Without a session the portal asks MindRoom Chat, the window that opened it,
 * for a Matrix OpenID token and trades it for a session.
 *
 * @param children - Portal content shown after sign-in.
 */
export function ConnectionsSignIn({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const run = async () => {
      let next = resolveProbe(await probeSession(signal));
      if (signal.aborted) return;
      if (!next) {
        const signedIn = await signInWithMatrix(window, signal);
        if (signal.aborted) return;
        next = signedIn ? resolveProbe(await probeSession(signal)) : null;
        if (signal.aborted) return;
      }
      setGate(next ?? { state: "blocked", message: OPEN_FROM_CHAT });
    };
    void run().catch(() => {
      if (!signal.aborted)
        setGate({ state: "blocked", message: OPEN_FROM_CHAT });
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
