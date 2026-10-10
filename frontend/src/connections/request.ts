/** Replacement messages for callers outside the Connections portal. */
export interface RequestErrorMessages {
  forbidden?: string;
  notFound?: string;
  /** Show a 403 response's own `detail` when it has one, with `forbidden` as the fallback. */
  forbiddenDetail?: boolean;
}

// Conflicts, oversized bodies, and invalid bodies explain themselves.
const USER_FACING_DETAIL_STATUSES = new Set([409, 413, 422]);

/**
 * Read an error `detail` that is either a string or FastAPI's list of
 * `{loc, msg}` items; the items' `input` is never read.
 */
function detailMessage(detail: unknown): string | null {
  if (typeof detail === "string") return detail || null;
  if (!Array.isArray(detail)) return null;
  const messages = detail.flatMap((item: unknown) => {
    if (typeof item !== "object" || item === null) return [];
    const { loc, msg } = item as { loc?: unknown; msg?: unknown };
    if (typeof msg !== "string" || !msg) return [];
    const where = Array.isArray(loc)
      ? loc
          .filter(
            (part) => typeof part === "string" || typeof part === "number",
          )
          .filter((part, index) => !(index === 0 && part === "body"))
          .join(".")
      : "";
    return [where ? `${where}: ${msg}` : msg];
  });
  return messages.length ? messages.join("; ") : null;
}

async function readDetail(response: Response): Promise<unknown> {
  return response
    .json()
    .then((payload: { detail?: unknown }) => payload.detail)
    .catch(() => null);
}

/**
 * Send a same-origin Connections API request and parse its JSON response.
 *
 * @param path - API path to request.
 * @param signal - Abort signal for canceling the request.
 * @param method - HTTP method.
 * @param body - JSON object for POST and PUT requests, empty by default.
 * @param messages - Optional wording for 403 and 404 responses. The `detail`
 * of a 409, 413, or 422 response is shown as is, and so is a 403's when
 * `forbiddenDetail` is set.
 * @returns A `Promise<T>` that resolves to the parsed response payload, or
 * `undefined` for an empty `204` response.
 */
export async function requestConnection<T>(
  path: string,
  signal: AbortSignal,
  method = "GET",
  body: object = {},
  messages: RequestErrorMessages = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      signal,
      credentials: "same-origin",
      ...(method === "POST" || method === "PUT"
        ? {
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          }
        : {}),
    });
  } catch {
    throw new Error("Could not reach the server. Try again.");
  }
  if (response.status === 401)
    throw new Error(
      "Your session has expired. Reload this page to sign in again.",
    );
  if (response.status === 403) {
    const detail = messages.forbiddenDetail ? await readDetail(response) : null;
    throw new Error(
      (typeof detail === "string" && detail) ||
        (messages.forbidden ??
          "Connections are not available for this account."),
    );
  }
  if (response.status === 404 && messages.notFound)
    throw new Error(messages.notFound);
  if (USER_FACING_DETAIL_STATUSES.has(response.status)) {
    // These messages are written for the user and never echo submitted values.
    const message = detailMessage(await readDetail(response));
    if (message) throw new Error(message);
  }
  if (!response.ok)
    throw new Error("Could not complete the request. Try again.");
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error(
      "Could not read the server response. Reload this page to try again.",
    );
  }
}
