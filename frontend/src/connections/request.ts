export async function requestConnection<T>(
  path: string,
  signal: AbortSignal,
  method = "GET",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      method,
      signal,
      credentials: "same-origin",
      ...(method === "POST"
        ? { headers: { "Content-Type": "application/json" }, body: "{}" }
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
    throw new Error("Connections are not available for this account.");
  if (!response.ok)
    throw new Error("Could not complete the request. Try again.");
  try {
    return (await response.json()) as T;
  } catch {
    throw new Error(
      "Could not read the server response. Reload this page to try again.",
    );
  }
}
