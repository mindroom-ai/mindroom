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
