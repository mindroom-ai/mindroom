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

export interface Agent {
  id: string; // The key in the agents object
  display_name: string;
  role: string;
  tools: string[];
  instructions: string[];
  rooms: string[];
  num_history_runs: number;
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
  confidence_threshold: number;
}

export interface VoiceConfig {
  enabled: boolean;
  stt: VoiceSTTConfig;
  intelligence: VoiceLLMConfig;
}

export interface Config {
  memory: MemoryConfig;
  models: Record<string, ModelConfig>;
  agents: Record<string, Omit<Agent, 'id'>>;
  defaults: {
    num_history_runs: number;
    markdown: boolean;
    add_history_to_messages: boolean;
  };
  router: {
    model: string;
  };
  room_models?: Record<string, string>; // Room-specific model overrides for teams
  teams?: Record<string, Omit<Team, 'id'>>; // Teams configuration
  tools?: Record<string, any>; // Tool configurations
  voice?: VoiceConfig; // Voice configuration
}

export interface APIKey {
  provider: string;
  key: string;
  isEncrypted: boolean;
}
