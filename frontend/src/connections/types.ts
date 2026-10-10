export interface ConnectionService {
  provider: string;
  is_shared: boolean;
  can_manage: boolean;
  display_name: string;
  description: string;
  icon: string | null;
  tools: string[];
}

export interface AgentConnections {
  agent_name: string;
  agent_display_name: string;
  is_shared: boolean;
  can_use: boolean;
  services: ConnectionService[];
  tools: ConnectionTool[];
  /**
   * The agent's egress services: an array, empty when it has none, for an agent
   * the viewer may use egress credentials with. Absent or `null` for any other.
   */
  egress_services?: EgressCredentialService[] | null;
}

export interface ConnectionTool {
  name: string;
  display_name: string;
  description: string;
  icon: string | null;
  provider: string | null;
  requires_room_context: boolean;
}

export interface ConnectionList {
  agents: AgentConnections[];
}

export interface ConnectionStatus {
  provider: string;
  connected: boolean;
  can_connect: boolean;
  reset_required: boolean;
  account_label: string | null;
}

/** The account a service can use instead of an API key. */
export interface EgressOAuthStatus {
  provider: string;
  display_name: string;
  connected: boolean;
  account_label: string | null;
  can_connect: boolean;
  reset_required: boolean;
  /** A shared service account serves this provider, so personal accounts are not connectable. */
  service_account: boolean;
  /**
   * `shared_sandbox` when the broker never uses a personal account here because
   * several users share the agent's sandbox; nothing is connected or connectable.
   */
  unavailable_reason: "shared_sandbox" | null;
  /** The service allows personal accounts in this shared sandbox anyway, so every user of the agent can act with one. */
  shared_worker_opt_in: boolean;
}

/** Who defined a service: the administrator in config.yaml or the caller's own scope. */
export type EgressServiceSource = "config" | "user";

/** Where a rule applies; the server never sends how it authenticates. */
export interface EgressRuleSummary {
  host: string;
  /** `null` matches any port. */
  port: number | null;
  path_prefix: string;
}

export interface EgressCredentialService {
  name: string;
  display_name: string;
  description: string;
  /** Absent for responses that do not say who defined the service. */
  source?: EgressServiceSource;
  /** Where the service applies, in rule order. Absent in responses from before the server sent it. */
  rules?: EgressRuleSummary[];
  /** `null` when the caller cannot tell whether the key is shared. */
  is_shared: boolean | null;
  /** Set by the dashboard for the global key that every agent without a worker scope shares. */
  is_global?: boolean;
  can_manage: boolean;
  /** True when either an API key or a connected account is available. */
  configured: boolean;
  updated_at: string | null;
  /** Which source the broker uses; an explicit key wins over a connected account. */
  active_source: "key" | "oauth" | null;
  key_configured: boolean;
  key_updated_at: string | null;
  oauth: EgressOAuthStatus | null;
}

/** Why one of the caller's own stored services is not in use. */
export type EgressInactiveReason = "shadowed" | "invalid";

/** An entry in the agent's own store that the broker ignores: an administrator's service has its name, or it no longer validates. */
export interface EgressInactiveService {
  name: string;
  reason: EgressInactiveReason;
}

export interface EgressCredentialAgent {
  agent_name: string;
  agent_display_name: string;
  /** A shared or unscoped agent: everyone using it shares its services and keys, and personal accounts are off. */
  shared: boolean;
  /** The caller may add, change, and delete services of this agent, even when it has none yet. */
  can_manage: boolean;
  services: EgressCredentialService[];
  /** Absent in responses from before the server listed them. */
  inactive_services?: EgressInactiveService[];
}

export interface EgressCredentialList {
  agents: EgressCredentialAgent[];
}

export type EgressAuthType = "bearer" | "basic" | "header" | "query";

/** How a rule adds the secret; `username` is for basic, `name` for header and query. */
export interface EgressAuth {
  type: EgressAuthType;
  username?: string;
  name?: string;
  template?: string;
}

export interface EgressRule {
  host: string;
  port?: number;
  path_prefix?: string;
  auth: EgressAuth;
}

/** A service as written in config.yaml or stored for a user: a preset stays `{preset: "github"}`. */
export interface AuthoredEgressService {
  preset?: string;
  display_name?: string | null;
  description?: string;
  rules?: EgressRule[];
  placeholder_env?: Record<string, string>;
  oauth_provider?: string | null;
  /** Operator-only; the personal API rejects it. */
  oauth_on_shared_workers?: boolean;
  restrict_to_rules?: boolean;
}

/** A built-in service preset as the server expands it. */
export interface EgressPreset {
  id: string;
  display_name: string;
  description: string;
  /** The OAuth provider the preset signs in with, if any. */
  oauth_provider: string | null;
  rules: EgressRuleSummary[];
  placeholder_env: Record<string, string>;
}

/** One row of the request log. */
export interface EgressLogRecord {
  at: string;
  /** `denied` rows were refused by the broker. */
  kind?: string;
  agent_name: string | null;
  method: string;
  host: string;
  path: string;
  service: string | null;
  status: number;
  /** The error code the broker refused with; `null` for a request it forwarded. */
  code: string | null;
}
