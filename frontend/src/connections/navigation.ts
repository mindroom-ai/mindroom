import type { MouseEvent } from "react";

/**
 * Open a link to another Connections page in this page, which keeps its
 * sign-in: a full load would run MindRoom Chat's sign-in handshake again.
 * `main.tsx` renders the page for the new address on `popstate`.
 * Clicks meant for a new tab or window are left to the browser.
 */
export function openInPage(event: MouseEvent<HTMLAnchorElement>) {
  if (
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  )
    return;
  event.preventDefault();
  // A second entry for the open page would only make Back seem to do nothing once.
  if (new URL(event.currentTarget.href).pathname === window.location.pathname)
    return;
  window.history.pushState(null, "", event.currentTarget.href);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
