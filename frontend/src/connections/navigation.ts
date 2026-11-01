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

const AGENT_LINK_PATH = /^\/api\/oauth\/[^/]+\/(authorize|reset)$/;

/**
 * The agent's connect or reset link MindRoom Chat asked the portal to open
 * once signed in (`?next=`), as a path on this origin, or `null`. Anything
 * else is refused, so the portal never sends its user to another page.
 */
export function agentLinkToOpen(search: string): string | null {
  const next = new URLSearchParams(search).get("next");
  if (!next) return null;
  let url: URL;
  try {
    url = new URL(next, window.location.origin);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  if (!AGENT_LINK_PATH.test(url.pathname)) return null;
  return `${url.pathname}${url.search}`;
}

/** Leave the portal for an agent link, replacing the portal in history. */
export function openAgentLink(path: string) {
  // The link and the provider pages it leads to must not reach MindRoom Chat's tab.
  window.opener = null;
  window.location.replace(path);
}
