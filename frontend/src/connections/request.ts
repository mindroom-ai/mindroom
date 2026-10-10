/** Replacement messages for callers outside the Connections portal. */
export interface RequestErrorMessages {
  forbidden?: string;
  notFound?: string;
}

/**
 * Send a same-origin Connections API request and parse its JSON response.
 *
 * @param path - API path to request.
 * @param signal - Abort signal for canceling the request.
 * @param method - HTTP method.
 * @param body - JSON object for POST and PUT requests, empty by default.
 * @param messages - Optional wording for 403 and 404 responses.
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
  if (response.status === 403)
    throw new Error(
      messages.forbidden ?? "Connections are not available for this account.",
    );
  if (response.status === 404 && messages.notFound)
    throw new Error(messages.notFound);
  if (response.status === 422) {
    // Validation messages are written for the user and never echo submitted values.
    const detail = await response
      .json()
      .then((payload: { detail?: unknown }) => payload.detail)
      .catch(() => null);
    if (typeof detail === "string" && detail) throw new Error(detail);
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
