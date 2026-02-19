import type { PROVIDERS } from '@/lib/providers';

export type ProviderType = keyof typeof PROVIDERS;

export interface ModelConfig {
  provider: ProviderType;
  id: string;
  host?: string; // For ollama
  extra_kwargs?: Record<string, any>; // Additional provider-specific parameters
}

export interface MemoryConfig {
  embedder: {
    provider: string;
    config: {
      model: string;
      host?: string;
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
  git?: KnowledgeGitConfig;
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
  context_files?: string[]; // File paths read at agent init and prepended to role context
  memory_dir?: string; // Directory containing MEMORY.md and dated memory files
  markdown?: boolean; // Per-agent markdown override
  learning?: boolean; // Defaults to true when omitted
  learning_mode?: LearningMode; // Defaults to always when omitted
  model?: string; // Reference to a model in the models section
  show_tool_calls?: boolean; // Show tool call details inline in responses (defaults to true)
  sandbox_tools?: string[]; // Tool names to execute through sandbox proxy (overrides defaults)
  delegate_to?: string[]; // Agent names this agent can delegate tasks to
  thread_mode?: ThreadMode; // Conversation threading mode
  num_history_runs?: number | null; // Number of prior runs to include as history
  num_history_messages?: number | null; // Max messages from history (mutually exclusive with num_history_runs)
  compress_tool_results?: boolean; // Compress tool results in history
  enable_session_summaries?: boolean; // Enable session summaries for conversation compaction
  max_tool_calls_from_history?: number | null; // Max tool call messages replayed from history
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
    sandbox_tools?: string[]; // Tool names to sandbox by default for all agents
    tools?: string[];
    enable_streaming?: boolean;
    show_stop_button?: boolean;
    num_history_runs?: number | null;
    num_history_messages?: number | null;
    compress_tool_results?: boolean;
    enable_session_summaries?: boolean;
    max_tool_calls_from_history?: number | null;
  };
  router: {
    model: string;
  };
  room_models?: Record<string, string>; // Room-specific model overrides for teams
  teams?: Record<string, Omit<Team, 'id'>>; // Teams configuration
  tools?: Record<string, any>; // Tool configurations
  voice?: VoiceConfig; // Voice configuration
}
