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
  egress_services?: EgressCredentialService[];
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

export interface EgressCredentialService {
  name: string;
  display_name: string;
  description: string;
  /** Absent for responses that do not say who defined the service. */
  source?: EgressServiceSource;
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

export interface EgressCredentialAgent {
  agent_name: string;
  agent_display_name: string;
  services: EgressCredentialService[];
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
  display_name?: string;
  description?: string;
  rules?: EgressRule[];
  placeholder_env?: Record<string, string>;
  oauth_provider?: string | null;
  /** Operator-only; the personal API rejects it. */
  oauth_on_shared_workers?: boolean;
  restrict_to_rules?: boolean;
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
  /** Why the broker refused the request, when the API says. */
  code?: string | null;
}
