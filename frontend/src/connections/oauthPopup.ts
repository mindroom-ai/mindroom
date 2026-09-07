export interface OAuthAuthorization {
  provider: string;
  auth_url: string;
  completion_origin: string;
}

const OAUTH_FLOW_TIMEOUT_MS = 5 * 60 * 1000;

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/\.$/, "");
  if (
    normalized === "localhost" ||
    normalized.endsWith(".localhost") ||
    normalized === "[::1]"
  )
    return true;
  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255)
  );
}

function isAllowedOAuthUrl(url: URL, allowLoopbackHttp: boolean): boolean {
  return (
    url.protocol === "https:" ||
    (allowLoopbackHttp &&
      url.protocol === "http:" &&
      isLoopbackHostname(url.hostname))
  );
}

export async function connectWithPopup(
  provider: string,
  authorize: () => Promise<OAuthAuthorization>,
  signal: AbortSignal,
): Promise<void> {
  if (signal.aborted) throw new Error("Authorization was cancelled.");
  const pageUrl = new URL(window.location.href);
  const allowLoopbackHttp =
    pageUrl.protocol === "http:" && isLoopbackHostname(pageUrl.hostname);
  // Keep this before the first await so browsers retain the click's user activation.
  const popup = window.open("about:blank", "_blank", "width=500,height=700");
  if (!popup)
    throw new Error("The popup was blocked. Allow popups and try again.");

  return new Promise((resolve, reject) => {
    let finished = false;
    let expectedOrigin: string | null = null;
    const finish = (error?: Error) => {
      if (finished) return;
      finished = true;
      window.clearInterval(poll);
      window.clearTimeout(deadline);
      window.removeEventListener("message", onMessage);
      signal.removeEventListener("abort", onAbort);
      if (!popup.closed) popup.close();
      if (error) reject(error);
      else resolve();
    };
    const onAbort = () => finish(new Error("Authorization was cancelled."));
    const onMessage = (event: MessageEvent) => {
      if (
        expectedOrigin === null ||
        event.origin !== expectedOrigin ||
        event.source !== popup ||
        event.data === null ||
        typeof event.data !== "object"
      )
        return;
      const data = event.data as Record<string, unknown>;
      if (
        data.type === "mindroom:oauth-complete" &&
        data.provider === provider &&
        data.status === "connected"
      )
        finish();
    };
    const poll = window.setInterval(() => {
      if (popup.closed) onAbort();
    }, 500);
    const deadline = window.setTimeout(
      () => finish(new Error("Authorization timed out. Try connecting again.")),
      OAUTH_FLOW_TIMEOUT_MS,
    );
    window.addEventListener("message", onMessage);
    signal.addEventListener("abort", onAbort, { once: true });
    void authorize()
      .then((data) => {
        if (finished) return;
        const authUrl = new URL(data.auth_url);
        const completionUrl = new URL(data.completion_origin);
        if (
          data.provider !== provider ||
          !isAllowedOAuthUrl(authUrl, allowLoopbackHttp) ||
          !isAllowedOAuthUrl(completionUrl, allowLoopbackHttp)
        )
          throw new Error("Could not start the connection. Try again.");
        expectedOrigin = completionUrl.origin;
        popup.location.href = authUrl.href;
      })
      .catch((error) =>
        finish(
          error instanceof Error
            ? error
            : new Error("Could not start the connection. Try again."),
        ),
      );
  });
}
