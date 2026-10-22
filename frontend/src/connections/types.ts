export interface ConnectionService {
  provider: string;
  is_shared: boolean;
  can_manage: boolean;
  /** Whether the account is connected, as the viewer's status would show it, read without refreshing the token. */
  connected: boolean;
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
  /**
   * A shared or unscoped agent's credential managers (Matrix IDs); empty for any
   * other agent, and for an agent with none only administrators manage it.
   */
  managers: string[];
  mcp_available: boolean;
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
  mcp_available: boolean;
}

export interface ConnectionList {
  agents: AgentConnections[];
}

/** Who the Connections pages are open for, from `GET /api/connections/me`. */
export interface ConnectionsIdentity {
  matrix_user_id: string;
  /** The Matrix display name, or the localpart when the profile has none. */
  display_name: string;
  email: string | null;
  /** The administrator dashboard is open to this user too. */
  is_administrator: boolean;
  /** The egress broker is on, so the Agent Keys page exists. */
  egress_enabled: boolean;
}

export interface ConnectionStatus {
  provider: string;
  connected: boolean;
  can_connect: boolean;
  reset_required: boolean;
  account_label: string | null;
  /** A shared service account serves this provider, so no personal account can be connected. */
  service_account: boolean;
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
  /**
   * Agent Keys may use this connection for the service. Until someone allows
   * it, a connected account serves only its tools.
   */
  reuse_enabled: boolean;
}

/** Who defined a service: the administrator in config.yaml or the caller's own scope. */
export type EgressServiceSource = "config" | "user";

/**
 * How a rule sends the key, in the listing and in the editor's form. A custom
 * template is header auth with a value template, which the authored format
 * does not tell apart; a listed rule of a restrict-only service, which sends
 * nothing, reads `null`.
 */
export type EgressRuleAuthType = EgressAuthType | "template";

/** Where a rule applies and how it sends the key, never the key itself. */
export interface EgressRuleSummary {
  host: string;
  /** `null` matches any port. */
  port: number | null;
  path_prefix: string;
  /** Sent for a service's rules; a preset's rules leave this and `auth_name` out. */
  auth_type?: EgressRuleAuthType | null;
  /** The header or query parameter the key goes in; `null` for any other way to send it. */
  auth_name?: string | null;
}

export interface EgressCredentialService {
  name: string;
  display_name: string;
  description: string;
  source: EgressServiceSource;
  /** Where the service applies, in rule order. */
  rules: EgressRuleSummary[];
  /**
   * The service only limits which paths a request may reach: its rules never
   * pick a credential, and it has no key or account.
   */
  restrict_only: boolean;
  /**
   * The names of the variables the service sets in the sandbox, a preset's
   * included, so a service of one's own that sets the same one can be warned
   * about those of an administrator's. Sent for every service in the personal
   * listing; absent on the dashboard.
   */
  placeholder_names?: string[];
  is_shared: boolean;
  /** Set by the dashboard for the global key that every agent without a worker scope shares. */
  is_global?: boolean;
  can_manage: boolean;
  /** True when either an API key or a connected account is available. */
  configured: boolean;
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

/**
 * Whose store an agent's services and keys live in: one `user_agent` agent's
 * own, every `user` agent's of the caller, everyone's on a `shared` agent, or
 * the global store of an `unscoped` one.
 */
export type EgressScope = "user" | "user_agent" | "shared" | "unscoped";

export interface EgressCredentialAgent {
  agent_name: string;
  agent_display_name: string;
  /** A shared or unscoped agent: everyone using it shares its services and keys, and personal accounts are off. */
  shared: boolean;
  scope: EgressScope;
  /** For a `user` agent, the display names of the caller's agents that share its store, this one included; empty otherwise. */
  scope_agents: string[];
  /** The Matrix IDs of a shared or unscoped agent's credential managers; empty for any other, and for an agent with none only administrators manage it. */
  managers: string[];
  /** The caller may add, change, and delete services of this agent, even when it has none yet. */
  can_manage: boolean;
  services: EgressCredentialService[];
  inactive_services: EgressInactiveService[];
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
  /** Null, like no port, means any port. */
  port?: number | null;
  path_prefix?: string;
  /** Required unless the service is restrict-only, whose rules send nothing and may leave it out. */
  auth?: EgressAuth;
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
  /** Only limits paths: no key, account, or placeholders, and its rules never pick a credential. */
  restrict_only?: boolean;
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

/** An account login the editor offers for a service. */
export interface EgressOAuthProviderOption {
  id: string;
  display_name: string;
}

/** The rules for the variables a service may set in the sandbox, as the server enforces them. */
export interface EgressPlaceholderValidation {
  /** The names a personal service's variable may have, as a regular expression source that must match the whole name. */
  name_pattern: string;
  /** A personal service's variable needs one of these as a word of its name; in the order the server's message lists them. */
  credential_words: string[];
  /** No word of a personal service's variable may be one of these; in the order the server's message lists them. */
  refused_words: string[];
  /** A personal service's variable may not start with this. */
  refused_prefix: string;
  /** Names no service may use, the broker sets them itself. */
  reserved_names: string[];
  /** Prefixes no service's variable may start with. */
  reserved_prefixes: string[];
  /** The values a personal service may set, as a regular expression source that must match the whole value. */
  value_pattern: string;
  /** The longest value `value_pattern` accepts. */
  value_max_length: number;
}

/** What the server checks when a service is saved; the editor checks the same before it asks. */
export interface EgressValidation {
  max_rules_per_service: number;
  /** The most one personal service takes, as the server counts its serialized JSON. */
  max_service_bytes: number;
  placeholder: EgressPlaceholderValidation;
}

/** What the presets endpoints return. */
export interface EgressPresetCatalog {
  presets: EgressPreset[];
  /** The providers a service can sign in with. */
  oauth_providers: EgressOAuthProviderOption[];
  validation: EgressValidation;
}

/** One row of the request log. */
export interface EgressLogRecord {
  at: string;
  /** `denied` rows were refused by the broker. */
  kind?: string;
  agent_name: string | null;
  /** Who made the request; `null` when it had no requester. */
  requester_id: string | null;
  method: string;
  host: string;
  path: string;
  service: string | null;
  status: number;
  /** The error code the broker refused with; `null` for a request it forwarded. */
  code: string | null;
}
