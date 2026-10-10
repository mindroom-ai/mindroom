import { Fragment, useEffect, useState, type ReactNode } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { signInWithMatrix, type MatrixSignInResult } from "./matrixSignIn";

const SESSION_PATH = "/api/connections/session";
const OPEN_FROM_CHAT =
  "Open Connections from MindRoom Chat (Settings, General) to sign in.";
const NOT_AVAILABLE = "Connections are not available for this account.";
const NOT_ENABLED = "Connections are not enabled on this server.";
const TRY_AGAIN = "Could not sign in. Try again.";
const REJECTED = "MindRoom did not accept the sign-in from MindRoom Chat.";

type Gate =
  | { state: "checking" }
  | { state: "signed-in"; user: string }
  | { state: "blocked"; message: string; retry?: boolean };

const blocked = (message: string): Gate => ({ state: "blocked", message });
const TRY_AGAIN_GATE: Gate = {
  state: "blocked",
  message: TRY_AGAIN,
  retry: true,
};

async function signedInUser(response: Response): Promise<string | null> {
  try {
    const body = (await response.json()) as { matrix_user_id?: unknown };
    return typeof body.matrix_user_id === "string" ? body.matrix_user_id : null;
  } catch {
    return null;
  }
}

/** Ask the server who is signed in; `null` means the browser has no session yet. */
async function probeSession(signal: AbortSignal): Promise<Gate | null> {
  const response = await fetch(SESSION_PATH, {
    method: "GET",
    signal,
    credentials: "same-origin",
  });
  if (response.status === 401) return null;
  if (response.status === 403) return blocked(NOT_AVAILABLE);
  if (response.status === 404) return blocked(NOT_ENABLED);
  const user = response.ok ? await signedInUser(response) : null;
  return user ? { state: "signed-in", user } : TRY_AGAIN_GATE;
}

const isRejection = (result: MatrixSignInResult) =>
  result.status === 401 || result.status === 403;

function signInFailure(result: MatrixSignInResult): Gate {
  // Without a status, Chat never answered, so only opening the portal from Chat can help.
  if (result.status === undefined) return blocked(OPEN_FROM_CHAT);
  if (isRejection(result)) return blocked(result.detail ?? REJECTED);
  if (result.status === 404) return blocked(NOT_ENABLED);
  return TRY_AGAIN_GATE;
}

/**
 * Render the portal for the Matrix user signed in to MindRoom Chat.
 *
 * When MindRoom Chat opened the portal, the portal asks it for a Matrix OpenID token and trades it for a session,
 * even if the browser still holds a session, so the account signed in to Chat replaces another account's session.
 *
 * @param children - Portal content shown after sign-in, remounted when the signed-in user changes.
 */
export function ConnectionsSignIn({ children }: { children: ReactNode }) {
  const [gate, setGate] = useState<Gate>({ state: "checking" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const run = async () => {
      const probed = await probeSession(signal);
      if (signal.aborted) return;
      const hasSession = probed?.state === "signed-in";
      if (probed) setGate(probed);
      if (probed && !hasSession) return;
      if (!window.opener) {
        if (!hasSession) setGate(blocked(OPEN_FROM_CHAT));
        return;
      }
      const result = await signInWithMatrix(window, signal);
      if (signal.aborted) return;
      if (!result.ok) {
        // An existing session survives a silent or unreachable Chat, but not a rejected Chat account.
        if (!hasSession || isRejection(result)) setGate(signInFailure(result));
        return;
      }
      const next = (await probeSession(signal)) ?? TRY_AGAIN_GATE;
      if (signal.aborted) return;
      // The key on the portal content remounts it only when the signed-in user changes.
      setGate(next);
    };
    void run().catch(() => {
      if (!signal.aborted) setGate(TRY_AGAIN_GATE);
    });
    return () => controller.abort();
  }, [attempt]);

  if (gate.state === "signed-in")
    return (
      <>
        <div className="bg-muted/20 px-4 pt-4 sm:px-6">
          <p className="mx-auto max-w-6xl text-sm text-muted-foreground">
            Signed in as {gate.user}
          </p>
        </div>
        <Fragment key={gate.user}>{children}</Fragment>
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
            <AlertDescription>
              <p>{gate.message}</p>
              {gate.retry ? (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  onClick={() => {
                    setGate({ state: "checking" });
                    setAttempt((count) => count + 1);
                  }}
                >
                  Try again
                </Button>
              ) : null}
            </AlertDescription>
          </Alert>
        )}
      </div>
    </main>
  );
}
