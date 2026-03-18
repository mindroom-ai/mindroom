import type { PROVIDERS } from '@/lib/providers';

export type ProviderType = keyof typeof PROVIDERS;
export type MemoryBackend = 'mem0' | 'file';
export type WorkerScope = 'shared' | 'user' | 'user_agent';
export type PrivateWorkerScope = Exclude<WorkerScope, 'shared'>;
export type AgentPolicySource =
  | 'private.per'
  | 'agent.worker_scope'
  | 'defaults.worker_scope'
  | 'unscoped';
export type AgentPoliciesByAgent = Record<string, AgentPolicy>;
export const DEFAULT_PRIVATE_KNOWLEDGE_PATH = 'memory';
export const SHARED_CONTEXT_FILE_PLACEHOLDER = 'SOUL.md';

export interface ModelConfig {
  provider: ProviderType;
  id: string;
  host?: string; // For ollama
  extra_kwargs?: Record<string, unknown>; // Additional provider-specific parameters
}

export interface MemoryConfig {
  backend?: MemoryBackend;
  team_reads_member_memory?: boolean;
  embedder: {
    provider: string;
    config: {
      model: string;
      host?: string;
      dimensions?: number;
    };
  };
  file?: {
    path?: string | null;
    max_entrypoint_lines?: number;
  };
  auto_flush?: {
    enabled?: boolean;
    flush_interval_seconds?: number;
    idle_seconds?: number;
    max_dirty_age_seconds?: number;
    stale_ttl_seconds?: number;
    max_cross_session_reprioritize?: number;
    retry_cooldown_seconds?: number;
    max_retry_cooldown_seconds?: number;
    batch?: {
      max_sessions_per_cycle?: number;
      max_sessions_per_agent_per_cycle?: number;
    };
    extractor?: {
      no_reply_token?: string;
      max_messages_per_flush?: number;
      max_chars_per_flush?: number;
      max_extraction_seconds?: number;
      include_memory_context?: {
        memory_snippets?: number;
        snippet_max_chars?: number;
      };
    };
  };
}

export interface KnowledgeGitConfig {
  repo_url: string;
  branch?: string;
  poll_interval_seconds?: number;
  credentials_service?: string;
  skip_hidden?: boolean;
  include_patterns?: string[];
  exclude_patterns?: string[];
}

export interface KnowledgeBaseConfig {
  path: string;
  watch: boolean;
  chunk_size?: number;
  chunk_overlap?: number;
  git?: KnowledgeGitConfig;
}

export interface AgentPrivateKnowledgeConfig {
  enabled?: boolean;
  path?: string | null;
  watch?: boolean;
  chunk_size?: number;
  chunk_overlap?: number;
  git?: KnowledgeGitConfig | null;
}

export interface AgentPrivateConfig {
  per: PrivateWorkerScope;
  root?: string | null;
  template_dir?: string | null;
  context_files?: string[] | null;
  knowledge?: AgentPrivateKnowledgeConfig | null;
}

export type LearningMode = 'always' | 'agentic';
export type CultureMode = 'automatic' | 'agentic' | 'manual';

export type ThreadMode = 'thread' | 'room';

export interface Agent {
  id: string; // The key in the agents object
  display_name: string;
  role: string;
  tools: string[];
  include_default_tools?: boolean; // Whether to merge defaults.tools into this agent's tools
  skills: string[];
  instructions: string[];
  rooms: string[];
  knowledge_bases?: string[];
  context_files?: string[]; // Workspace-relative files loaded into each freshly built agent instance
  markdown?: boolean; // Per-agent markdown override
  learning?: boolean; // Defaults to true when omitted
  learning_mode?: LearningMode; // Defaults to always when omitted
  memory_backend?: MemoryBackend; // Per-agent memory backend override (inherits memory.backend when omitted)
  model?: string; // Reference to a model in the models section
  show_tool_calls?: boolean; // Show tool call details inline in responses (defaults to true)
  worker_tools?: string[]; // Tool names to route through scoped workers (overrides defaults)
  worker_scope?: WorkerScope | null;
  private?: AgentPrivateConfig | null;
  delegate_to?: string[]; // Agent names this agent can delegate tasks to
  thread_mode?: ThreadMode; // Conversation threading mode
  room_thread_modes?: Record<string, ThreadMode>; // Room-specific thread mode overrides
  num_history_runs?: number | null; // Number of prior runs to include as history
  num_history_messages?: number | null; // Max messages from history (mutually exclusive with num_history_runs)
  compress_tool_results?: boolean; // Compress tool results in history
  enable_session_summaries?: boolean; // Enable session summaries for conversation compaction
  max_tool_calls_from_history?: number | null; // Max tool call messages replayed from history
  allow_self_config?: boolean; // Allow agent to modify its own configuration via a tool
}

export interface Team {
  id: string; // The key in the teams object
  display_name: string;
  role: string;
  agents: string[]; // List of agent IDs
  rooms: string[];
  mode: 'coordinate' | 'collaborate';
  model?: string; // Optional team-specific model
}

export interface Culture {
  id: string; // The key in the cultures object
  description: string;
  agents: string[]; // List of agent IDs
  mode: CultureMode;
}

export interface Room {
  id: string; // Room identifier
  display_name: string;
  description?: string;
  agents: string[]; // List of agent IDs in this room
  model?: string; // Room-specific model override
}

export interface VoiceSTTConfig {
  provider: string;
  model: string;
  api_key?: string;
  host?: string;
}

export interface VoiceLLMConfig {
  model: string;
}

export interface VoiceConfig {
  enabled: boolean;
  visible_router_echo: boolean;
  stt: VoiceSTTConfig;
  intelligence: VoiceLLMConfig;
}

export interface Config {
  memory: MemoryConfig;
  knowledge_bases?: Record<string, KnowledgeBaseConfig>;
  cultures?: Record<string, Omit<Culture, 'id'>>; // Culture configurations
  models: Record<string, ModelConfig>;
  agents: Record<string, Omit<Agent, 'id'>>;
  defaults: {
    markdown: boolean;
    learning?: boolean;
    learning_mode?: LearningMode;
    show_tool_calls?: boolean;
    worker_scope?: WorkerScope | null;
    worker_tools?: string[]; // Tool names to route through scoped workers by default for all agents
    tools?: string[];
    enable_streaming?: boolean;
    show_stop_button?: boolean;
    num_history_runs?: number | null;
    num_history_messages?: number | null;
    compress_tool_results?: boolean;
    enable_session_summaries?: boolean;
    max_tool_calls_from_history?: number | null;
    allow_self_config?: boolean;
  };
  router: {
    model: string;
  };
  room_models?: Record<string, string>; // Room-specific model overrides for teams
  teams?: Record<string, Omit<Team, 'id'>>; // Teams configuration
  tools?: Record<string, unknown>; // Tool configurations
  voice?: VoiceConfig; // Voice configuration
}

export interface AgentPolicy {
  agent_name: string;
  is_private: boolean;
  effective_execution_scope: WorkerScope | null;
  scope_label: string;
  scope_source: AgentPolicySource;
  dashboard_credentials_supported: boolean;
  team_eligibility_reason: string | null;
  private_knowledge_base_id: string | null;
  request_scoped_workspace_enabled: boolean;
  request_scoped_knowledge_enabled: boolean;
}

function normalizePrivateKnowledgeConfig(
  knowledge: AgentPrivateKnowledgeConfig | null | undefined
): AgentPrivateKnowledgeConfig | null | undefined {
  if (knowledge == null) {
    return knowledge;
  }

  const trimmedPath = knowledge.path?.trim();
  if (knowledge.enabled === true) {
    return {
      ...knowledge,
      path: trimmedPath && trimmedPath.length > 0 ? trimmedPath : DEFAULT_PRIVATE_KNOWLEDGE_PATH,
    };
  }

  return {
    ...knowledge,
    path: trimmedPath && trimmedPath.length > 0 ? trimmedPath : undefined,
  };
}

function normalizePrivateConfig(
  privateConfig: AgentPrivateConfig | null | undefined
): AgentPrivateConfig | null | undefined {
  if (privateConfig == null) {
    return privateConfig;
  }

  return {
    ...privateConfig,
    per: privateConfig.per ?? 'user',
    knowledge: normalizePrivateKnowledgeConfig(privateConfig.knowledge),
  };
}

export function getDefaultPrivateConfig(agent: Pick<Agent, 'private'>): AgentPrivateConfig {
  if (agent.private != null) {
    return agent.private;
  }
  return {
    per: 'user',
  };
}

export function normalizeAgentUpdates(agent: Agent, updates: Partial<Agent>): Partial<Agent> {
  const normalizedUpdates: Partial<Agent> = { ...updates };
  const nextPrivate = 'private' in updates ? updates.private : agent.private;

  if (nextPrivate != null) {
    normalizedUpdates.private = normalizePrivateConfig(nextPrivate);
    normalizedUpdates.worker_scope = undefined;
  }

  return normalizedUpdates;
}
