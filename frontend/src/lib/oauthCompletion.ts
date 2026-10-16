interface OAuthCompletionOptions {
  provider: string;
  expectedOrigin: () => string | null;
  pollIntervalMs: number;
  /** What a popup that was closed or answered `cancelled` reports. */
  cancellationMessage: string;
  /** What a popup that answered `error` reports. */
  errorMessage?: string;
  /**
   * Asked once when the popup is found closed without an answer: it may have
   * connected and closed before its message was read. When it resolves true the
   * flow counts as connected; when it resolves false or fails, as cancelled.
   */
  confirmConnected?: () => Promise<boolean>;
  /**
   * Called once. `popupClosesItself` is true when the popup's own page
   * answered, and closes itself after a moment so that its text can be read.
   */
  onSettled: (error?: Error, popupClosesItself?: boolean) => void;
}

type Outcome = "connected" | "cancelled" | "error";

// The pages of the OAuth popup answer `connected`, `cancelled`, or `error`.
const OUTCOME_TYPE = "mindroom-oauth";
const OUTCOMES: readonly unknown[] = ["connected", "cancelled", "error"];

/** What the popup's page said, or null when the message is not from this popup, origin, and provider. */
function readOutcome(
  event: MessageEvent,
  popup: Window,
  provider: string,
  expectedOrigin: string | null,
): Outcome | null {
  if (
    expectedOrigin === null ||
    event.origin !== expectedOrigin ||
    event.source !== popup ||
    event.data === null ||
    typeof event.data !== "object"
  )
    return null;
  const data = event.data as Record<string, unknown>;
  if (data.provider !== provider) return null;
  if (data.type === OUTCOME_TYPE && OUTCOMES.includes(data.status))
    return data.status as Outcome;
  return null;
}

/** Own completion filtering, single settlement, and popup-observer cleanup. */
export function watchOAuthCompletion(
  popup: Window,
  options: OAuthCompletionOptions,
) {
  let finished = false;
  const finish = (error?: Error, popupClosesItself = false) => {
    if (finished) return;
    finished = true;
    window.clearInterval(poll);
    window.removeEventListener("message", onMessage);
    options.onSettled(error, popupClosesItself);
  };
  const onMessage = (event: MessageEvent) => {
    const outcome = readOutcome(
      event,
      popup,
      options.provider,
      options.expectedOrigin(),
    );
    if (outcome === null) return;
    finish(
      outcome === "connected"
        ? undefined
        : new Error(
            outcome === "cancelled"
              ? options.cancellationMessage
              : (options.errorMessage ?? "Authorization failed. Try again."),
          ),
      true, // The popup's own page answered, so it closes itself.
    );
  };
  const poll = window.setInterval(() => {
    if (!popup.closed) return;
    // Nothing more to see once it is closed, so ask only once.
    window.clearInterval(poll);
    const cancelled = new Error(options.cancellationMessage);
    if (!options.confirmConnected) {
      finish(cancelled);
      return;
    }
    options.confirmConnected().then(
      (connected) => finish(connected ? undefined : cancelled),
      () => finish(cancelled),
    );
  }, options.pollIntervalMs);
  window.addEventListener("message", onMessage);
  return {
    finish,
    get finished() {
      return finished;
    },
  };
}
