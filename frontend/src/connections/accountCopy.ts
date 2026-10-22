import type { EgressScope } from "./types";

/** Wording about accounts and keys that the Connections pages share. */

/** `a`, `a and b`, or `a, b, and c`. */
export function formatList(items: readonly string[]): string {
  if (items.length <= 2) return items.join(" and ");
  return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
}

export const SHARED_ACCOUNT_TITLE = "Connect a shared account?";

/** Asked before a Connect on a shared agent: whoever uses the agent acts as the account picked. */
export function sharedAccountBody(agent: string, provider: string): string {
  return `Everyone who uses ${agent} will act as the ${provider} account you pick. Use a team account, not your personal one.`;
}

/** How a connected account reads to everyone who uses a shared agent. */
export function sharedAccountStatus(
  accountLabel: string | null,
  agent: string,
): string {
  return `${accountLabel ? `Connected as ${accountLabel}` : "Connected"} · shared with everyone using ${agent}`;
}

/** What Disconnect or Reset removes, and which services stop working with it. */
export function disconnectDescription(
  provider: string,
  agent: string,
  services: readonly string[],
): string {
  const removed = `This removes the saved ${provider} connection for ${agent}.`;
  return services.length > 0
    ? `${removed} These services stop working until an account is connected again: ${formatList(services)}.`
    : removed;
}

/**
 * Who can change the keys of a shared agent, for the note under its name.
 * Beside an account the user cannot change, `keysShown` false (the agent shows
 * no keys, as while Agent Keys is off) names its accounts instead.
 */
export function sharedAgentNote(
  canManage: boolean,
  managers: readonly string[],
  keysShown = true,
): string {
  if (canManage)
    return "You manage this shared agent's keys. Everyone who uses it shares them.";
  const who = managers.length > 0 ? formatList(managers) : "administrators";
  return keysShown
    ? `Only ${who} can change keys for this shared agent.`
    : `Only ${who} can change this shared agent's accounts.`;
}

/** Who uses a personal agent's keys, for the line under its name; null for a shared agent, whose managers are named instead. */
export function personalScopeLine(scope: EgressScope): string | null {
  if (scope === "user_agent") return "Only you use this agent's keys.";
  if (scope === "user")
    return "Shared by all your agents in your personal workspace.";
  return null;
}

/** Why a personal account is no use on a shared agent, and what to do instead. */
export function personalAccountRefusal(
  provider: string,
  canManage: boolean,
  managers: readonly string[],
): string {
  const why = `Personal ${provider} accounts can't be used by shared agents.`;
  if (canManage) return `${why} Add a team API key instead.`;
  const who = managers.length > 0 ? formatList(managers) : "an administrator";
  return `${why} Ask ${who} to add a team API key.`;
}

/**
 * Why a personal account is no use for an agent that is not shared, whose
 * sandbox runs other users' commands too, and what to do instead.
 */
export function personalSandboxRefusal(provider: string): string {
  return `Personal ${provider} accounts can't be used in this sandbox, which other users' commands share. Add an API key instead.`;
}

/** What a service that refuses other paths on a host also does to the administrator's services there, and for whom. */
export function administratorLimitNote(scope: EgressScope): string {
  const reach = {
    user_agent: "for this agent only",
    user: "for all your agents that share your personal workspace",
    shared: "for everyone using this agent",
    unscoped: "for every agent without a scope",
  }[scope];
  return `This also limits services from your administrator on these hosts, ${reach}.`;
}

/** What removing a limit gives back, to whom, by the scope its store has. */
export function removeLimitWarning(
  scope: EgressScope,
  agent: string,
  agents: readonly string[],
): string {
  const who = {
    user_agent: `Your ${agent}`,
    user: `All your agents that share your personal workspace (${agents.join(", ")})`,
    shared: `Everyone using ${agent}`,
    unscoped: "Every agent without a scope",
  }[scope];
  return `This removes the limit. ${who} can then reach every repository the key or connected account allows.`;
}

/**
 * What deleting a service takes away from whom, by the scope its store has.
 * `agents` names the agents that share a `user` scope's store, this one included.
 */
export function deleteServiceWarning(
  scope: EgressScope,
  agent: string,
  agents: readonly string[],
): string {
  if (scope === "user_agent")
    return `This deletes the service and its saved key. Your ${agent} loses access through it.`;
  if (scope === "user")
    return `This deletes the service and its saved key. All your agents that share your personal workspace lose access through it: ${agents.join(", ")}.`;
  if (scope === "unscoped")
    return "This deletes the service and its shared key. Every agent without a scope loses access through it.";
  return `This deletes the service and its shared key. Everyone using ${agent} loses access through it.`;
}

/** The `Passed through` text for a tunnel the broker only relayed. */
export const PASSED_THROUGH = "Passed through (no key added)";

/**
 * The request log's caption: who else can see the requests, which on a shared
 * or unscoped agent includes its credential managers, and whether the table
 * holds only the viewer's requests or, for one of those managers, everyone's.
 */
export function passThroughCaption(shared: boolean, everyone: boolean): string {
  const viewers = shared
    ? "Administrators and this agent's credential managers"
    : "Administrators";
  const shown = everyone
    ? "Requests from everyone who uses this agent are shown."
    : "Only your own requests are shown.";
  return `Requests to sites without a service pass straight through; MindRoom sees only the host. ${viewers} can also see these requests (method, host, and path). ${shown}`;
}
