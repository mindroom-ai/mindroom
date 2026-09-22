interface OAuthCompletionOptions {
  provider: string;
  expectedOrigin: () => string | null;
  pollIntervalMs: number;
  cancellationMessage: string;
  onSettled: (error?: Error) => void;
}

function isOAuthCompleteMessage(
  event: MessageEvent,
  popup: Window,
  provider: string,
  expectedOrigin: string | null,
): boolean {
  if (
    expectedOrigin === null ||
    event.origin !== expectedOrigin ||
    event.source !== popup ||
    event.data === null ||
    typeof event.data !== "object"
  )
    return false;
  const data = event.data as Record<string, unknown>;
  return (
    data.type === "mindroom:oauth-complete" &&
    data.provider === provider &&
    data.status === "connected"
  );
}

/** Own completion filtering, single settlement, and popup-observer cleanup. */
export function watchOAuthCompletion(
  popup: Window,
  options: OAuthCompletionOptions,
) {
  let finished = false;
  const finish = (error?: Error) => {
    if (finished) return;
    finished = true;
    window.clearInterval(poll);
    window.removeEventListener("message", onMessage);
    options.onSettled(error);
  };
  const onMessage = (event: MessageEvent) => {
    if (
      isOAuthCompleteMessage(
        event,
        popup,
        options.provider,
        options.expectedOrigin(),
      )
    )
      finish();
  };
  const poll = window.setInterval(() => {
    if (popup.closed) finish(new Error(options.cancellationMessage));
  }, options.pollIntervalMs);
  window.addEventListener("message", onMessage);
  return {
    finish,
    get finished() {
      return finished;
    },
  };
}
