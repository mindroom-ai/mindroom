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

export interface Agent {
  id: string; // The key in the agents object
  display_name: string;
  role: string;
  tools: string[];
  skills: string[];
  instructions: string[];
  rooms: string[];
  knowledge_bases?: string[];
  learning?: boolean; // Defaults to true when omitted
  learning_mode?: LearningMode; // Defaults to always when omitted
  model?: string; // Reference to a model in the models section
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
  };
  router: {
    model: string;
  };
  room_models?: Record<string, string>; // Room-specific model overrides for teams
  teams?: Record<string, Omit<Team, 'id'>>; // Teams configuration
  tools?: Record<string, any>; // Tool configurations
  voice?: VoiceConfig; // Voice configuration
}
