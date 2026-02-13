import { create } from 'zustand';
import {
  Config,
  Agent,
  Team,
  Room,
  ModelConfig,
  KnowledgeBaseConfig,
  Culture,
} from '@/types/config';
import * as configService from '@/services/configService';

function unassignAgentsFromOtherCultures(
  cultures: Culture[],
  targetCultureId: string,
  targetCultureAgents: string[]
): Culture[] {
  const assignedAgents = new Set(targetCultureAgents);
  return cultures.map(culture => {
    if (culture.id === targetCultureId) {
      return culture;
    }
    return {
      ...culture,
      agents: culture.agents.filter(agentId => !assignedAgents.has(agentId)),
    };
  });
}

interface ConfigState {
  // State
  config: Config | null;
  agents: Agent[];
  teams: Team[];
  cultures: Culture[];
  rooms: Room[];
  selectedAgentId: string | null;
  selectedTeamId: string | null;
  selectedCultureId: string | null;
  selectedRoomId: string | null;
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
  selectCulture: (cultureId: string | null) => void;
  updateCulture: (cultureId: string, updates: Partial<Culture>) => void;
  createCulture: (culture: Omit<Culture, 'id'>) => void;
  deleteCulture: (cultureId: string) => void;
  selectRoom: (roomId: string | null) => void;
  updateRoom: (roomId: string, updates: Partial<Room>) => void;
  createRoom: (room: Omit<Room, 'id'>) => void;
  deleteRoom: (roomId: string) => void;
  addAgentToRoom: (roomId: string, agentId: string) => void;
  removeAgentFromRoom: (roomId: string, agentId: string) => void;
  updateRoomModels: (roomModels: Record<string, string>) => void;
  updateMemoryConfig: (memoryConfig: { provider: string; model: string; host?: string }) => void;
  updateKnowledgeBase: (baseName: string, baseConfig: KnowledgeBaseConfig) => void;
  deleteKnowledgeBase: (baseName: string) => void;
  updateModel: (modelId: string, updates: Partial<ModelConfig>) => void;
  deleteModel: (modelId: string) => void;
  updateToolConfig: (toolId: string, config: any) => void;
  markDirty: () => void;
  clearError: () => void;
}

export const useConfigStore = create<ConfigState>((set, get) => ({
  // Initial state
  config: null,
  agents: [],
  teams: [],
  cultures: [],
  rooms: [],
  selectedAgentId: null,
  selectedTeamId: null,
  selectedCultureId: null,
  selectedRoomId: null,
  isDirty: false,
  isLoading: false,
  error: null,
  syncStatus: 'disconnected',

  // Load configuration from backend
  loadConfig: async () => {
    set({ isLoading: true, error: null });
    try {
      const config = await configService.loadConfig();
      const normalizedConfig: Config = {
        ...config,
        knowledge_bases: config.knowledge_bases || {},
        cultures: config.cultures || {},
      };
      const defaultLearning = config.defaults?.learning ?? true;
      const defaultLearningMode = config.defaults?.learning_mode ?? 'always';
      const agents = Object.entries(normalizedConfig.agents).map(([id, agent]) => ({
        id,
        ...agent,
        skills: agent.skills ?? [],
        knowledge_bases: agent.knowledge_bases || [],
        learning: agent.learning ?? defaultLearning,
        learning_mode: agent.learning_mode ?? defaultLearningMode,
      }));
      const teams = normalizedConfig.teams
        ? Object.entries(normalizedConfig.teams).map(([id, team]) => ({
            id,
            ...team,
          }))
        : [];
      const cultures = normalizedConfig.cultures
        ? Object.entries(normalizedConfig.cultures).map(([id, culture]) => ({
            id,
            ...culture,
            agents: culture.agents ?? [],
            mode: culture.mode ?? 'automatic',
            description: culture.description ?? '',
          }))
        : [];

      // Extract unique rooms from agents and create Room objects
      const roomIds = new Set<string>();
      agents.forEach(agent => {
        agent.rooms.forEach(room => roomIds.add(room));
      });

      const rooms: Room[] = Array.from(roomIds).map(roomId => {
        const agentsInRoom = agents.filter(agent => agent.rooms.includes(roomId)).map(a => a.id);
        const roomModel = config.room_models?.[roomId];
        return {
          id: roomId,
          display_name: roomId.charAt(0).toUpperCase() + roomId.slice(1),
          description: '',
          agents: agentsInRoom,
          model: roomModel,
        };
      });

      set({
        config: normalizedConfig,
        agents,
        teams,
        cultures,
        rooms,
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
    const { config, agents, teams, cultures, rooms } = get();
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
      const culturesObject = cultures.reduce(
        (acc, culture) => {
          const { id, ...rest } = culture;
          acc[id] = rest;
          return acc;
        },
        {} as Record<string, Omit<Culture, 'id'>>
      );

      // Extract room_models from rooms that have a model set
      const roomModels: Record<string, string> = {};
      rooms.forEach(room => {
        if (room.model) {
          roomModels[room.id] = room.model;
        }
      });

      const updatedConfig: Config = {
        ...config,
        agents: agentsObject,
        teams: teamsObject,
        cultures: culturesObject,
        room_models: Object.keys(roomModels).length > 0 ? roomModels : undefined,
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
    const defaultLearning = get().config?.defaults.learning ?? true;
    const defaultLearningMode = get().config?.defaults.learning_mode ?? 'always';
    const newAgent: Agent = {
      id,
      ...agentData,
      knowledge_bases: agentData.knowledge_bases ?? [],
      learning: agentData.learning ?? defaultLearning,
      learning_mode: agentData.learning_mode ?? defaultLearningMode,
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
      cultures: state.cultures.map(culture => ({
        ...culture,
        agents: culture.agents.filter(id => id !== agentId),
      })),
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

  // Select a culture for editing
  selectCulture: cultureId => {
    set({ selectedCultureId: cultureId });
  },

  // Update an existing culture
  updateCulture: (cultureId, updates) => {
    set(state => {
      const updatedCultures = state.cultures.map(culture =>
        culture.id === cultureId ? { ...culture, ...updates } : culture
      );

      if (updates.agents) {
        const targetCulture = updatedCultures.find(culture => culture.id === cultureId);
        if (!targetCulture) {
          return { cultures: updatedCultures, isDirty: true };
        }
        return {
          cultures: unassignAgentsFromOtherCultures(
            updatedCultures,
            cultureId,
            targetCulture.agents
          ),
          isDirty: true,
        };
      }

      return {
        cultures: updatedCultures,
        isDirty: true,
      };
    });
  },

  // Create a new culture
  createCulture: cultureData => {
    set(state => {
      const baseId = (cultureData.description || 'new_culture').toLowerCase().replace(/\s+/g, '_');
      let id = baseId;
      let counter = 1;
      while (state.cultures.some(culture => culture.id === id)) {
        id = `${baseId}_${counter}`;
        counter += 1;
      }

      const newCulture: Culture = {
        id,
        ...cultureData,
        description: cultureData.description || '',
        mode: cultureData.mode || 'automatic',
        agents: cultureData.agents || [],
      };
      const nextCultures = unassignAgentsFromOtherCultures(
        [...state.cultures, newCulture],
        id,
        newCulture.agents
      );
      return {
        cultures: nextCultures,
        selectedCultureId: id,
        isDirty: true,
      };
    });
  },

  // Delete a culture
  deleteCulture: cultureId => {
    set(state => ({
      cultures: state.cultures.filter(culture => culture.id !== cultureId),
      selectedCultureId: state.selectedCultureId === cultureId ? null : state.selectedCultureId,
      isDirty: true,
    }));
  },

  // Select a room for editing
  selectRoom: roomId => {
    set({ selectedRoomId: roomId });
  },

  // Update an existing room
  updateRoom: (roomId, updates) => {
    set(state => {
      const updatedRooms = state.rooms.map(room =>
        room.id === roomId ? { ...room, ...updates } : room
      );

      let updatedConfig = state.config;

      // If model changed, update room_models in config
      if (updates.model !== undefined && state.config) {
        const currentRoomModels = state.config.room_models || {};
        const newRoomModels = { ...currentRoomModels };

        if (updates.model) {
          // Set the room model
          newRoomModels[roomId] = updates.model;
        } else {
          // Remove the room model if it's being unset
          delete newRoomModels[roomId];
        }

        updatedConfig = {
          ...state.config,
          room_models: Object.keys(newRoomModels).length > 0 ? newRoomModels : undefined,
        };
      }

      // If agents changed, update the agents' rooms arrays
      if (updates.agents) {
        const oldRoom = state.rooms.find(r => r.id === roomId);
        const oldAgents = oldRoom?.agents || [];
        const newAgents = updates.agents;

        // Remove room from agents no longer in the room
        const removedAgents = oldAgents.filter(id => !newAgents.includes(id));
        // Add room to new agents
        const addedAgents = newAgents.filter(id => !oldAgents.includes(id));

        const updatedAgents = state.agents.map(agent => {
          if (removedAgents.includes(agent.id)) {
            return { ...agent, rooms: agent.rooms.filter(r => r !== roomId) };
          }
          if (addedAgents.includes(agent.id) && !agent.rooms.includes(roomId)) {
            return { ...agent, rooms: [...agent.rooms, roomId] };
          }
          return agent;
        });

        return {
          config: updatedConfig,
          rooms: updatedRooms,
          agents: updatedAgents,
          isDirty: true,
        };
      }

      return {
        config: updatedConfig,
        rooms: updatedRooms,
        isDirty: true,
      };
    });
  },

  // Create a new room
  createRoom: roomData => {
    const id = roomData.display_name.toLowerCase().replace(/\s+/g, '_');
    const newRoom: Room = {
      id,
      ...roomData,
    };

    set(state => {
      // Add room to selected agents
      const updatedAgents = state.agents.map(agent => {
        if (roomData.agents.includes(agent.id) && !agent.rooms.includes(id)) {
          return { ...agent, rooms: [...agent.rooms, id] };
        }
        return agent;
      });

      return {
        rooms: [...state.rooms, newRoom],
        agents: updatedAgents,
        selectedRoomId: id,
        isDirty: true,
      };
    });
  },

  // Delete a room
  deleteRoom: roomId => {
    set(state => {
      // Remove room from all agents
      const updatedAgents = state.agents.map(agent => ({
        ...agent,
        rooms: agent.rooms.filter(r => r !== roomId),
      }));

      // Remove room from teams
      const updatedTeams = state.teams.map(team => ({
        ...team,
        rooms: team.rooms.filter(r => r !== roomId),
      }));

      // Remove from room_models if it exists
      let updatedConfig = state.config;
      if (state.config?.room_models?.[roomId]) {
        const { [roomId]: _, ...remainingModels } = state.config.room_models;
        updatedConfig = {
          ...state.config,
          room_models: remainingModels,
        };
      }

      return {
        rooms: state.rooms.filter(room => room.id !== roomId),
        agents: updatedAgents,
        teams: updatedTeams,
        config: updatedConfig,
        selectedRoomId: state.selectedRoomId === roomId ? null : state.selectedRoomId,
        isDirty: true,
      };
    });
  },

  // Add agent to room
  addAgentToRoom: (roomId, agentId) => {
    set(state => {
      const updatedRooms = state.rooms.map(room => {
        if (room.id === roomId && !room.agents.includes(agentId)) {
          return { ...room, agents: [...room.agents, agentId] };
        }
        return room;
      });

      const updatedAgents = state.agents.map(agent => {
        if (agent.id === agentId && !agent.rooms.includes(roomId)) {
          return { ...agent, rooms: [...agent.rooms, roomId] };
        }
        return agent;
      });

      return {
        rooms: updatedRooms,
        agents: updatedAgents,
        isDirty: true,
      };
    });
  },

  // Remove agent from room
  removeAgentFromRoom: (roomId, agentId) => {
    set(state => {
      const updatedRooms = state.rooms.map(room => {
        if (room.id === roomId) {
          return { ...room, agents: room.agents.filter(id => id !== agentId) };
        }
        return room;
      });

      const updatedAgents = state.agents.map(agent => {
        if (agent.id === agentId) {
          return { ...agent, rooms: agent.rooms.filter(r => r !== roomId) };
        }
        return agent;
      });

      return {
        rooms: updatedRooms,
        agents: updatedAgents,
        isDirty: true,
      };
    });
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

  // Update one knowledge base configuration
  updateKnowledgeBase: (baseName, baseConfig) => {
    set(state => {
      if (!state.config) return state;
      return {
        config: {
          ...state.config,
          knowledge_bases: {
            ...(state.config.knowledge_bases || {}),
            [baseName]: {
              ...baseConfig,
            },
          },
        },
        isDirty: true,
      };
    });
  },

  // Delete a knowledge base and unassign it from agents
  deleteKnowledgeBase: baseName => {
    set(state => {
      if (!state.config) return state;

      const knowledgeBases = { ...(state.config.knowledge_bases || {}) };
      delete knowledgeBases[baseName];

      const agents = state.agents.map(agent => ({
        ...agent,
        knowledge_bases: (agent.knowledge_bases || []).filter(base => base !== baseName),
      }));
      const configAgents = Object.fromEntries(
        Object.entries(state.config.agents).map(([agentId, agentConfig]) => [
          agentId,
          {
            ...agentConfig,
            knowledge_bases: (agentConfig.knowledge_bases || []).filter(base => base !== baseName),
          },
        ])
      );

      return {
        config: {
          ...state.config,
          knowledge_bases: knowledgeBases,
          agents: configAgents,
        },
        agents,
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
