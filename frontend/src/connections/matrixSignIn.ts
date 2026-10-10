const READY_MESSAGE = "mindroom:connections-ready";
const OPENID_MESSAGE = "mindroom:connections-openid";
const SESSION_PATH = "/api/connections/session";

/**
 * Ask the window that opened this portal for a Matrix OpenID token.
 *
 * The ready message carries no secret, so it goes to any origin. The backend
 * checks the reply's origin against its allowlist before trusting the token.
 *
 * @param win - Window whose opener is asked and whose messages are read.
 * @param timeoutMs - How long to wait for the opener's answer.
 * @param signal - Optional signal that stops waiting early.
 * @returns The token with the origin that sent it, or `null` when there is no opener, no answer in time, or the wait was aborted.
 */
export async function requestOpenIdFromOpener(
  win: Window = window,
  timeoutMs = 30000,
  signal?: AbortSignal,
): Promise<{ openidToken: unknown; origin: string } | null> {
  const opener: Window | null = win.opener;
  if (!opener || signal?.aborted) return null;
  return new Promise((resolve) => {
    const finish = (
      result: { openidToken: unknown; origin: string } | null,
    ) => {
      clearTimeout(timer);
      win.removeEventListener("message", onMessage);
      signal?.removeEventListener("abort", onAbort);
      resolve(result);
    };
    const onAbort = () => finish(null);
    const onMessage = (event: MessageEvent) => {
      if (event.source !== opener) return;
      const data: unknown = event.data;
      if (
        typeof data !== "object" ||
        data === null ||
        (data as { type?: unknown }).type !== OPENID_MESSAGE
      )
        return;
      const openidToken = (data as { openid_token?: unknown }).openid_token;
      if (
        typeof openidToken !== "object" ||
        openidToken === null ||
        Array.isArray(openidToken)
      )
        return;
      finish({ openidToken, origin: event.origin });
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    win.addEventListener("message", onMessage);
    signal?.addEventListener("abort", onAbort);
    opener.postMessage({ type: READY_MESSAGE }, "*");
  });
}

/**
 * Exchange an OpenID token from the opener for a Connections session cookie.
 *
 * @param win - Window whose opener supplies the token.
 * @param signal - Optional signal that stops waiting for the opener.
 * @returns `true` when the server created the session, otherwise `false`.
 */
export async function signInWithMatrix(
  win: Window = window,
  signal?: AbortSignal,
): Promise<boolean> {
  const answer = await requestOpenIdFromOpener(win, undefined, signal);
  if (!answer) return false;
  try {
    const response = await fetch(SESSION_PATH, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        openid_token: answer.openidToken,
        client_origin: answer.origin,
      }),
    });
    return response.ok;
  } catch {
    return false;
  }
}
