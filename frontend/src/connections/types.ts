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
}

export interface EgressCredentialService {
  name: string;
  display_name: string;
  description: string;
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
