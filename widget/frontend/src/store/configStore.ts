import { create } from 'zustand';
import { Config, Agent, ModelConfig, APIKey } from '@/types/config';
import * as configService from '@/services/configService';

interface ConfigState {
  // State
  config: Config | null;
  agents: Agent[];
  selectedAgentId: string | null;
  apiKeys: Record<string, APIKey>;
  isDirty: boolean;
  isLoading: boolean;
  error: string | null;
  syncStatus: 'synced' | 'syncing' | 'error' | 'disconnected';

  // Actions
  loadConfig: () => Promise<void>;
  saveConfig: () => Promise<void>;
  selectAgent: (agentId: string | null) => void;
  updateAgent: (agentId: string, updates: Partial<Agent>) => void;
  createAgent: (agent: Omit<Agent, 'id'>) => void;
  deleteAgent: (agentId: string) => void;
  updateModel: (modelId: string, updates: Partial<ModelConfig>) => void;
  deleteModel: (modelId: string) => void;
  setAPIKey: (provider: string, key: string) => void;
  testModel: (modelId: string) => Promise<boolean>;
  updateToolConfig: (toolId: string, config: any) => void;
  markDirty: () => void;
  clearError: () => void;
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  // Initial state
  config: null,
  agents: [],
  selectedAgentId: null,
  apiKeys: {},
  isDirty: false,
  isLoading: false,
  error: null,
  syncStatus: 'disconnected',

  // Load configuration from backend
  loadConfig: async () => {
    set({ isLoading: true, error: null });
    try {
      const config = await configService.loadConfig();
      const agents = Object.entries(config.agents).map(([id, agent]) => ({
        id,
        ...agent,
      }));
      set({
        config,
        agents,
        isLoading: false,
        syncStatus: 'synced',
        isDirty: false,
      });
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : 'Failed to load config',
        isLoading: false,
        syncStatus: 'error',
      });
    }
  },

  // Save configuration to backend
  saveConfig: async () => {
    const { config, agents } = get();
    if (!config) return;

    set({ isLoading: true, error: null, syncStatus: 'syncing' });
    try {
      // Convert agents array back to object format
      const agentsObject = agents.reduce(
        (acc, agent) => {
          const { id, ...rest } = agent;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Agent, 'id'>>
      );

      const updatedConfig: Config = {
        ...config,
        agents: agentsObject,
      };

      await configService.saveConfig(updatedConfig);
      set({
        isLoading: false,
        syncStatus: 'synced',
        isDirty: false,
      });
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : 'Failed to save config',
        isLoading: false,
        syncStatus: 'error',
      });
    }
  },

  // Select an agent for editing
  selectAgent: agentId => {
    set({ selectedAgentId: agentId });
  },

  // Update an existing agent
  updateAgent: (agentId, updates) => {
    set(state => ({
      agents: state.agents.map(agent => (agent.id === agentId ? { ...agent, ...updates } : agent)),
      isDirty: true,
    }));
  },

  // Create a new agent
  createAgent: agentData => {
    const id = agentData.display_name.toLowerCase().replace(/\s+/g, '_');
    const newAgent: Agent = {
      id,
      ...agentData,
    };
    set(state => ({
      agents: [...state.agents, newAgent],
      selectedAgentId: id,
      isDirty: true,
    }));
  },

  // Delete an agent
  deleteAgent: agentId => {
    set(state => ({
      agents: state.agents.filter(agent => agent.id !== agentId),
      selectedAgentId: state.selectedAgentId === agentId ? null : state.selectedAgentId,
      isDirty: true,
    }));
  },

  // Update a model configuration
  updateModel: (modelId, updates) => {
    set(state => {
      if (!state.config) return state;
      return {
        config: {
          ...state.config,
          models: {
            ...state.config.models,
            [modelId]: {
              ...state.config.models[modelId],
              ...updates,
            },
          },
        },
        isDirty: true,
      };
    });
  },

  // Delete a model configuration
  deleteModel: modelId => {
    set(state => {
      if (!state.config) return state;
      const { [modelId]: _, ...remainingModels } = state.config.models;
      return {
        config: {
          ...state.config,
          models: remainingModels,
        },
        isDirty: true,
      };
    });
  },

  // Set an API key
  setAPIKey: (provider, key) => {
    set(state => ({
      apiKeys: {
        ...state.apiKeys,
        [provider]: {
          provider,
          key,
          isEncrypted: false,
        },
      },
    }));
  },

  // Test a model connection
  testModel: async modelId => {
    try {
      const result = await configService.testModel(modelId);
      return result;
    } catch (error) {
      console.error('Failed to test model:', error);
      return false;
    }
  },

  // Update tool configuration
  updateToolConfig: (toolId, config) => {
    set(state => {
      if (!state.config) return state;
      return {
        config: {
          ...state.config,
          tools: {
            ...state.config.tools,
            [toolId]: config,
          },
        },
        isDirty: true,
      };
    });
  },

  // Mark configuration as dirty
  markDirty: () => {
    set({ isDirty: true });
  },

  // Clear error
  clearError: () => {
    set({ error: null });
  },
}));
