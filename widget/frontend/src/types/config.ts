export interface ModelConfig {
  provider: 'openai' | 'anthropic' | 'ollama' | 'openrouter';
  id: string;
  host?: string; // For ollama
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
}

export interface APIKey {
  provider: string;
  key: string;
  isEncrypted: boolean;
}

// Available tools based on the config
export const AVAILABLE_TOOLS = [
  'calculator',
  'file',
  'shell',
  'python',
  'csv',
  'pandas',
  'yfinance',
  'arxiv',
  'duckduckgo',
  'googlesearch',
  'tavily',
  'wikipedia',
  'newspaper',
  'website',
  'jina',
  'docker',
  'github',
  'email',
  'telegram',
] as const;

export type ToolType = (typeof AVAILABLE_TOOLS)[number];
