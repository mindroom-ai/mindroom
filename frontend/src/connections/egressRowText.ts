import { formatList } from "./accountCopy";
import { GRAPHQL_CAVEAT, limitedTo, parseGithubLimit } from "./githubLimits";
import { githubLimitLabels } from "./githubRepositoryRules";
import type {
  EgressCredentialService,
  EgressOAuthStatus,
  EgressRuleSummary,
  EgressScope,
  EgressServiceSource,
} from "./types";

/**
 * The agent a list of rows belongs to, for the wording that depends on it.
 */
export interface RowAgent {
  displayName: string;
  /** A shared or unscoped agent: everyone using it shares its keys and accounts. */
  shared: boolean;
  /** Whose store its keys and accounts live in; every `unscoped` agent shares the global one. */
  scope?: EgressScope;
  /** The credential managers' Matrix IDs; the dashboard, whose operator manages every service, leaves them out. */
  managers?: string[];
  /** Every service of the agent: disconnecting a provider names the ones that use it. */
  services: EgressCredentialService[];
}

/** Controls for the services a viewer defines themselves, and a label for the ones someone else defined. */
export interface ServiceEditing {
  /**
   * Services with this `source` get Edit and Delete, when the viewer can
   * manage them; a limit of that source gets Change limit and Remove limit.
   */
  editableSource: EgressServiceSource;
  /** Turns Edit and Delete off while something else is being written. */
  disabled?: boolean;
  /** The service whose editor is open: opening it again would only offer to discard what is typed there. */
  editingName?: string | null;
  onEdit: (service: EgressCredentialService) => void;
  /** Rejects with an Error whose message the row shows. */
  onDelete: (service: EgressCredentialService) => Promise<void>;
  deleteWarning: (service: EgressCredentialService) => string;
  /** Label for services of another source; the personal page groups its rows instead. */
  labels?: Partial<Record<EgressServiceSource, string>>;
  /**
   * Offers Limit to repositories on an administrator's service that reaches
   * GitHub, when the viewer can manage the agent.
   */
  onLimitToRepositories?: (service: EgressCredentialService) => void;
  /** Turns Limit to repositories off while the editor is open. */
  limitDisabled?: boolean;
}

/** Whether a service or limit gets its edit actions: it has the `editableSource` and the viewer manages it. */
export function canEdit(
  editing: ServiceEditing,
  service: EgressCredentialService | null,
): boolean {
  return (
    service?.can_manage === true && service.source === editing.editableSource
  );
}

/**
 * Whether connecting this account makes everyone who uses the agent act as it.
 * Not so for a personal account (the agent is not shared), a service account,
 * or a requester's own account that a sandbox shared by several users refuses
 * or only uses under the service's opt-in.
 */
export function sharesAccount(
  agent: RowAgent,
  oauth: EgressOAuthStatus | null,
): boolean {
  return (
    agent.shared &&
    oauth !== null &&
    !oauth.service_account &&
    !oauth.shared_worker_opt_in &&
    oauth.unavailable_reason === null
  );
}

function keySavedLabel(saved: string | null): string {
  const date = saved ? new Date(saved) : null;
  return date && !Number.isNaN(date.getTime())
    ? `Key saved ${date.toLocaleDateString()}`
    : "Key saved";
}

/** What the row's status label says about where the service's credential comes from. */
export function statusLabel(service: EgressCredentialService): string {
  const oauth = service.oauth;
  if (oauth) {
    // An account that cannot be used here leaves the key as all there is to say.
    if (service.active_source === "key")
      return oauth.unavailable_reason === null
        ? "Using API key"
        : keySavedLabel(service.key_updated_at);
    // The row names the account below the status label.
    if (service.active_source === "oauth") return "Using connected account";
    if (oauth.connected && !oauth.reuse_enabled)
      return "Connected for tools only";
    return oauth.reset_required ? "Reset required" : "Not set";
  }
  if (!service.configured) return "Not set";
  return keySavedLabel(service.key_updated_at);
}

export function removeWarning(service: EgressCredentialService): string {
  if (service.is_global)
    return "This deletes the global key, which every agent without a worker scope shares. Those agents lose access to this service until a key is set again.";
  // A shared agent's key, or for an agent without a worker scope the global
  // key, so other agents may rely on it too.
  if (service.is_shared)
    return "This deletes the shared key. Everyone who relies on it loses access to this service until a key is set again.";
  return "This deletes your saved key. Your agent loses access to this service until you set a key again.";
}

/** The names of the services that sign in with this provider's account, for the disconnect question. */
export function servicesUsingAccount(
  services: readonly EgressCredentialService[],
  provider: string,
): string[] {
  return services
    .filter((other) => other.oauth?.provider === provider)
    .map((other) => other.display_name);
}

/**
 * "Limited to a/b" for a service whose rules reach only some repositories,
 * unless its description already says so. A limit that allows GraphQL adds
 * that GraphQL reaches every repository the key can reach.
 */
export function limitedNote(service: EgressCredentialService): string | null {
  const limit = limitedTo(service.rules);
  if (limit === null) return null;
  const caveat = limit.graphql ? ` (${GRAPHQL_CAVEAT})` : "";
  const text = githubLimitLabels(limit.repositories).description + caveat;
  return service.description === text ? null : text;
}

/** A rule's host, with the port when it names one. */
export const hostPort = (rule: EgressRuleSummary) =>
  `${rule.host}${rule.port === null ? "" : `:${rule.port}`}`;

/** The hosts a service's rules cover, each once, in rule order, with the port when a rule names one. */
export function serviceHosts(service: EgressCredentialService): string[] {
  return [...new Set(service.rules.map(hostPort))];
}

/** What happens to requests to its hosts while the service has no key or account; null once it has, and for a limit. */
export function unsetNote(service: EgressCredentialService): string | null {
  if (service.configured || service.restrict_only || service.rules.length === 0)
    return null;
  const oauth = service.oauth;
  // Where Agent Keys can use an account, the account stops refusals as well as a key does.
  if (oauth && oauth.unavailable_reason === null && !oauth.service_account) {
    if (oauth.connected && !oauth.reset_required)
      return "Requests are refused until a key is set or this account is used for Agent Keys.";
    if (oauth.can_connect || oauth.reset_required)
      return "Requests are refused until a key is set or an account is connected.";
  }
  return "Requests are refused until a key is set.";
}

/**
 * The repositories a limit names, as `a/b, c/*`, or "selected paths" when its
 * rules name none, followed by what GraphQL reaches when the limit allows it.
 */
export function limitTarget(limit: EgressCredentialService): string {
  const { repositories, graphql } = parseGithubLimit(limit.rules);
  const target =
    repositories.length > 0 ? repositories.join(", ") : "selected paths";
  return graphql ? `${target} (${GRAPHQL_CAVEAT})` : target;
}

/**
 * A note on the viewer's other services that are not limits and have rules on
 * these hosts, as "My GitHub also has rules on api.github.com. A request is
 * allowed when any of this agent's own services allows it.", or null when none
 * has. The broker lets a request past a limit when any rule of the agent's own
 * services (the user's, or on a shared agent its credential managers') matches
 * it. Only hosts are compared, not paths or ports, so the note does not say
 * whether the limit still refuses anything.
 */
export function limitOverlap(
  hosts: readonly string[],
  services: readonly EgressCredentialService[],
): string | null {
  const limited = new Set(hosts.map((host) => host.toLowerCase()));
  const sharedHosts = (other: EgressCredentialService) =>
    other.rules
      .map((rule) => rule.host.toLowerCase())
      .filter((host) => limited.has(host));
  const open = services.filter(
    (other) =>
      other.source === "user" &&
      !other.restrict_only &&
      sharedHosts(other).length > 0,
  );
  if (open.length === 0) return null;
  const names = formatList(open.map((other) => other.display_name));
  const allowed = formatList([...new Set(open.flatMap(sharedHosts))]);
  return `${names} also ${open.length === 1 ? "has" : "have"} rules on ${allowed}. A request is allowed when any of this agent's own services allows it.`;
}

/**
 * The limit that narrows an administrator's service: a service of the viewer's
 * that only restricts paths on one of its hosts. Null for any other service.
 */
export function limitOf(
  service: EgressCredentialService,
  services: readonly EgressCredentialService[],
): EgressCredentialService | null {
  if (service.source !== "config" || service.restrict_only) return null;
  const hosts = new Set(service.rules.map((rule) => rule.host.toLowerCase()));
  return (
    services.find(
      (other) =>
        other.restrict_only &&
        other.source === "user" &&
        other.rules.some((rule) => hosts.has(rule.host.toLowerCase())),
    ) ?? null
  );
}

/** How many of these services' keys and accounts are set, out of all of them; a limit has neither. */
export function keyCounts(services: readonly EgressCredentialService[]) {
  const keyed = services.filter((service) => !service.restrict_only);
  return {
    set: keyed.filter((service) => service.configured).length,
    total: keyed.length,
  };
}

/** "2 of 6 set", or "None" when there is no key or account to set. */
export function keysSetText(services: readonly EgressCredentialService[]) {
  const { set, total } = keyCounts(services);
  return total === 0 ? "None" : `${set} of ${total} set`;
}
