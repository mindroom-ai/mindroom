import { create } from 'zustand';
import { Config, Agent, Team, ModelConfig, APIKey } from '@/types/config';
import * as configService from '@/services/configService';

interface ConfigState {
  // State
  config: Config | null;
  agents: Agent[];
  teams: Team[];
  selectedAgentId: string | null;
  selectedTeamId: string | null;
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
  selectTeam: (teamId: string | null) => void;
  updateTeam: (teamId: string, updates: Partial<Team>) => void;
  createTeam: (team: Omit<Team, 'id'>) => void;
  deleteTeam: (teamId: string) => void;
  updateRoomModels: (roomModels: Record<string, string>) => void;
  updateMemoryConfig: (memoryConfig: { provider: string; model: string; host?: string }) => void;
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
  teams: [],
  selectedAgentId: null,
  selectedTeamId: null,
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
      const teams = config.teams
        ? Object.entries(config.teams).map(([id, team]) => ({
            id,
            ...team,
          }))
        : [];
      set({
        config,
        agents,
        teams,
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
    const { config, agents, teams } = get();
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

      // Convert teams array back to object format
      const teamsObject = teams.reduce(
        (acc, team) => {
          const { id, ...rest } = team;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Team, 'id'>>
      );

      const updatedConfig: Config = {
        ...config,
        agents: agentsObject,
        teams: teamsObject,
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

  // Select a team for editing
  selectTeam: teamId => {
    set({ selectedTeamId: teamId });
  },

  // Update an existing team
  updateTeam: (teamId, updates) => {
    set(state => ({
      teams: state.teams.map(team => (team.id === teamId ? { ...team, ...updates } : team)),
      isDirty: true,
    }));
  },

  // Create a new team
  createTeam: teamData => {
    const id = teamData.display_name.toLowerCase().replace(/\s+/g, '_');
    const newTeam: Team = {
      id,
      ...teamData,
    };
    set(state => ({
      teams: [...state.teams, newTeam],
      selectedTeamId: id,
      isDirty: true,
    }));
  },

  // Delete a team
  deleteTeam: teamId => {
    set(state => ({
      teams: state.teams.filter(team => team.id !== teamId),
      selectedTeamId: state.selectedTeamId === teamId ? null : state.selectedTeamId,
      isDirty: true,
    }));
  },

  // Update room models
  updateRoomModels: roomModels => {
    set(state => {
      if (!state.config) return state;
      return {
        config: {
          ...state.config,
          room_models: roomModels,
        },
        isDirty: true,
      };
    });
  },

  // Update memory configuration
  updateMemoryConfig: memoryConfig => {
    set(state => {
      if (!state.config) return state;
      return {
        config: {
          ...state.config,
          memory: {
            ...state.config.memory,
            embedder: {
              provider: memoryConfig.provider,
              config: {
                model: memoryConfig.model,
                ...(memoryConfig.host ? { host: memoryConfig.host } : {}),
              },
            },
          },
        },
        isDirty: true,
      };
    });
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
