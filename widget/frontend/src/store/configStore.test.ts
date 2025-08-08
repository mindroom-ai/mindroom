import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useConfigStore } from './configStore';
import type { Agent, Team, Config } from '@/types/config';

// Mock fetch globally
global.fetch = vi.fn();

describe('configStore', () => {
  beforeEach(() => {
    // Reset store state
    useConfigStore.setState({
      config: null,
      agents: [],
      selectedAgentId: null,
      isDirty: false,
      syncStatus: 'disconnected',
    });

    // Clear all mocks
    vi.clearAllMocks();
  });

  describe('loadConfig', () => {
    it('should load configuration successfully', async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: 'Test Agent',
            role: 'Test role',
            tools: ['calculator'],
            instructions: ['Test instruction'],
            rooms: ['lobby'],
            num_history_runs: 5,
          },
        },
        models: {
          default: {
            provider: 'ollama',
            id: 'test-model',
          },
        },
      };

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => mockConfig,
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.config).toEqual(mockConfig);
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe('test');
      expect(state.agents[0].display_name).toBe('Test Agent');
      expect(state.syncStatus).toBe('synced');
    });

    it('should handle load errors', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe('error');
    });
  });

  describe('saveConfig', () => {
    it('should save configuration successfully', async () => {
      // Set up initial state with agents array
      const mockConfig: Config = {
        agents: {
          test: {
            display_name: 'Test',
            role: 'Test role',
            tools: [],
            instructions: [],
            rooms: [],
            num_history_runs: 5,
          },
        },
        models: {},
        memory: {
          embedder: {
            provider: 'openai',
            config: {
              model: 'text-embedding-ada-002',
            },
          },
        },
        defaults: {
          num_history_runs: 5,
          markdown: true,
          add_history_to_messages: false,
        },
        router: {
          model: 'default',
        },
      };
      const mockAgents = [
        {
          id: 'test',
          display_name: 'Test',
          role: 'Test role',
          tools: [],
          instructions: [],
          rooms: [],
          num_history_runs: 5,
        },
      ];
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        isDirty: true,
        syncStatus: 'synced',
      });
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ success: true }),
      });

      const { saveConfig } = useConfigStore.getState();
      await saveConfig();

      // The saveConfig removes the id field when saving
      const { id, ...agentWithoutId } = mockAgents[0];
      expect(global.fetch).toHaveBeenCalledWith('/api/config/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...mockConfig,
          agents: { test: agentWithoutId },
          teams: {}, // saveConfig adds empty teams if not present
        }),
      });

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(false);
      expect(state.syncStatus).toBe('synced');
    });
  });

  describe('agent operations', () => {
    beforeEach(() => {
      // Set up agents
      const agents: Agent[] = [
        {
          id: 'agent1',
          display_name: 'Agent 1',
          role: 'Role 1',
          tools: [],
          instructions: [],
          rooms: [],
          num_history_runs: 5,
        },
        {
          id: 'agent2',
          display_name: 'Agent 2',
          role: 'Role 2',
          tools: ['calculator'],
          instructions: ['Test'],
          rooms: ['lobby'],
          num_history_runs: 5,
        },
      ];
      useConfigStore.setState({ agents });
    });

    it('should select agent', () => {
      const { selectAgent } = useConfigStore.getState();
      selectAgent('agent2');

      const state = useConfigStore.getState();
      expect(state.selectedAgentId).toBe('agent2');
    });

    it('should update agent', () => {
      const { updateAgent } = useConfigStore.getState();
      updateAgent('agent1', { display_name: 'Updated Agent' });

      const state = useConfigStore.getState();
      const updatedAgent = state.agents.find(a => a.id === 'agent1');
      expect(updatedAgent?.display_name).toBe('Updated Agent');
      expect(state.isDirty).toBe(true);
    });

    it('should create new agent', () => {
      const newAgentData = {
        display_name: 'New Agent',
        role: 'New role',
        tools: [],
        instructions: [],
        rooms: [],
        num_history_runs: 5,
      };

      const { createAgent } = useConfigStore.getState();
      createAgent(newAgentData);

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(3);
      const newAgent = state.agents[2];
      expect(newAgent.display_name).toBe('New Agent');
      expect(state.selectedAgentId).toBe(newAgent.id);
      expect(state.isDirty).toBe(true);
    });

    it('should delete agent', () => {
      const { deleteAgent } = useConfigStore.getState();
      deleteAgent('agent1');

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe('agent2');
      expect(state.isDirty).toBe(true);
    });
  });

  describe('dirty state', () => {
    it('should mark state as dirty', () => {
      const { markDirty } = useConfigStore.getState();
      markDirty();

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(true);
    });
  });

  describe('teams', () => {
    beforeEach(() => {
      const mockTeams: Team[] = [
        {
          id: 'team1',
          display_name: 'Team 1',
          role: 'Test team 1',
          agents: ['agent1', 'agent2'],
          rooms: ['room1'],
          mode: 'coordinate',
        },
        {
          id: 'team2',
          display_name: 'Team 2',
          role: 'Test team 2',
          agents: ['agent3'],
          rooms: ['room2'],
          mode: 'collaborate',
          model: 'gpt4',
        },
      ];

      useConfigStore.setState({
        teams: mockTeams,
        selectedTeamId: 'team1',
      });
    });

    it('should select team', () => {
      const { selectTeam } = useConfigStore.getState();
      selectTeam('team2');

      const state = useConfigStore.getState();
      expect(state.selectedTeamId).toBe('team2');
    });

    it('should update team', () => {
      const { updateTeam } = useConfigStore.getState();
      updateTeam('team1', { display_name: 'Updated Team' });

      const state = useConfigStore.getState();
      const updatedTeam = state.teams.find(t => t.id === 'team1');
      expect(updatedTeam?.display_name).toBe('Updated Team');
      expect(state.isDirty).toBe(true);
    });

    it('should create new team', () => {
      const { createTeam } = useConfigStore.getState();
      const newTeamData = {
        display_name: 'New Team',
        role: 'New team role',
        agents: ['agent1'],
        rooms: ['lobby'],
        mode: 'coordinate' as const,
      };

      createTeam(newTeamData);

      const state = useConfigStore.getState();
      expect(state.teams).toHaveLength(3);
      const newTeam = state.teams[2];
      expect(newTeam.display_name).toBe('New Team');
      expect(newTeam.id).toBe('new_team');
      expect(state.selectedTeamId).toBe('new_team');
      expect(state.isDirty).toBe(true);
    });

    it('should delete team', () => {
      const { deleteTeam } = useConfigStore.getState();
      deleteTeam('team1');

      const state = useConfigStore.getState();
      expect(state.teams).toHaveLength(1);
      expect(state.teams[0].id).toBe('team2');
      expect(state.selectedTeamId).toBe(null);
      expect(state.isDirty).toBe(true);
    });
  });

  describe('room models', () => {
    it('should update room models', () => {
      useConfigStore.setState({
        config: {
          memory: { embedder: { provider: 'openai', config: { model: 'test' } } },
          models: {},
          agents: {},
          defaults: {
            num_history_runs: 5,
            markdown: true,
            add_history_to_messages: true,
          },
          router: { model: 'default' },
        },
      });

      const { updateRoomModels } = useConfigStore.getState();
      const roomModels = {
        lobby: 'gpt4',
        dev: 'claude',
      };

      updateRoomModels(roomModels);

      const state = useConfigStore.getState();
      expect(state.config?.room_models).toEqual(roomModels);
      expect(state.isDirty).toBe(true);
    });
  });

  describe('memory config', () => {
    it('should update memory configuration', () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: {
                model: 'text-embedding-ada-002',
              },
            },
          },
          models: {},
          agents: {},
          defaults: {
            num_history_runs: 5,
            markdown: true,
            add_history_to_messages: true,
          },
          router: { model: 'default' },
        },
      });

      const { updateMemoryConfig } = useConfigStore.getState();
      const newMemoryConfig = {
        provider: 'ollama',
        model: 'nomic-embed-text',
        host: 'http://localhost:11434',
      };

      updateMemoryConfig(newMemoryConfig);

      const state = useConfigStore.getState();
      expect(state.config?.memory.embedder.provider).toBe('ollama');
      expect(state.config?.memory.embedder.config.model).toBe('nomic-embed-text');
      expect(state.config?.memory.embedder.config.host).toBe('http://localhost:11434');
      expect(state.isDirty).toBe(true);
    });

    it('should handle memory config without host', () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: {
                model: 'text-embedding-ada-002',
              },
            },
          },
          models: {},
          agents: {},
          defaults: {
            num_history_runs: 5,
            markdown: true,
            add_history_to_messages: true,
          },
          router: { model: 'default' },
        },
      });

      const { updateMemoryConfig } = useConfigStore.getState();
      const newMemoryConfig = {
        provider: 'openai',
        model: 'text-embedding-3-small',
      };

      updateMemoryConfig(newMemoryConfig);

      const state = useConfigStore.getState();
      expect(state.config?.memory.embedder.provider).toBe('openai');
      expect(state.config?.memory.embedder.config.model).toBe('text-embedding-3-small');
      expect(state.config?.memory.embedder.config.host).toBeUndefined();
    });
  });

  describe('rooms', () => {
    beforeEach(() => {
      const mockRooms = [
        {
          id: 'lobby',
          display_name: 'Lobby',
          description: 'Main room',
          agents: ['agent1'],
          model: 'default',
        },
        {
          id: 'dev',
          display_name: 'Dev Room',
          description: 'Development room',
          agents: ['agent2'],
        },
      ];

      const mockAgents = [
        {
          id: 'agent1',
          display_name: 'Agent 1',
          role: 'Test agent',
          tools: [],
          instructions: [],
          rooms: ['lobby'],
          num_history_runs: 5,
        },
        {
          id: 'agent2',
          display_name: 'Agent 2',
          role: 'Test agent 2',
          tools: [],
          instructions: [],
          rooms: ['dev'],
          num_history_runs: 5,
        },
      ];

      useConfigStore.setState({
        rooms: mockRooms,
        agents: mockAgents,
        selectedRoomId: 'lobby',
      });
    });

    it('should select room', () => {
      const { selectRoom } = useConfigStore.getState();
      selectRoom('dev');

      const state = useConfigStore.getState();
      expect(state.selectedRoomId).toBe('dev');
    });

    it('should update room', () => {
      const { updateRoom } = useConfigStore.getState();
      updateRoom('lobby', { display_name: 'Updated Lobby' });

      const state = useConfigStore.getState();
      const updatedRoom = state.rooms.find(r => r.id === 'lobby');
      expect(updatedRoom?.display_name).toBe('Updated Lobby');
      expect(state.isDirty).toBe(true);
    });

    it('should update agents when room agents change', () => {
      const { updateRoom } = useConfigStore.getState();
      updateRoom('lobby', { agents: ['agent1', 'agent2'] });

      const state = useConfigStore.getState();
      const agent2 = state.agents.find(a => a.id === 'agent2');
      expect(agent2?.rooms).toContain('lobby');
      expect(state.isDirty).toBe(true);
    });

    it('should create new room', () => {
      const { createRoom } = useConfigStore.getState();
      const newRoomData = {
        display_name: 'New Room',
        description: 'Test room',
        agents: ['agent1'],
      };

      createRoom(newRoomData);

      const state = useConfigStore.getState();
      expect(state.rooms).toHaveLength(3);
      const newRoom = state.rooms[2];
      expect(newRoom.display_name).toBe('New Room');
      expect(newRoom.id).toBe('new_room');
      expect(state.selectedRoomId).toBe('new_room');

      // Check that agent1 now has new_room in its rooms
      const agent1 = state.agents.find(a => a.id === 'agent1');
      expect(agent1?.rooms).toContain('new_room');
      expect(state.isDirty).toBe(true);
    });

    it('should delete room and update agents', () => {
      const { deleteRoom } = useConfigStore.getState();
      deleteRoom('lobby');

      const state = useConfigStore.getState();
      expect(state.rooms).toHaveLength(1);
      expect(state.rooms[0].id).toBe('dev');

      // Check that agent1 no longer has lobby in its rooms
      const agent1 = state.agents.find(a => a.id === 'agent1');
      expect(agent1?.rooms).not.toContain('lobby');
      expect(state.selectedRoomId).toBe(null);
      expect(state.isDirty).toBe(true);
    });

    it('should add agent to room', () => {
      const { addAgentToRoom } = useConfigStore.getState();
      addAgentToRoom('dev', 'agent1');

      const state = useConfigStore.getState();
      const devRoom = state.rooms.find(r => r.id === 'dev');
      expect(devRoom?.agents).toContain('agent1');

      const agent1 = state.agents.find(a => a.id === 'agent1');
      expect(agent1?.rooms).toContain('dev');
      expect(state.isDirty).toBe(true);
    });

    it('should remove agent from room', () => {
      const { removeAgentFromRoom } = useConfigStore.getState();
      removeAgentFromRoom('lobby', 'agent1');

      const state = useConfigStore.getState();
      const lobbyRoom = state.rooms.find(r => r.id === 'lobby');
      expect(lobbyRoom?.agents).not.toContain('agent1');

      const agent1 = state.agents.find(a => a.id === 'agent1');
      expect(agent1?.rooms).not.toContain('lobby');
      expect(state.isDirty).toBe(true);
    });
  });

  describe('saveConfig with teams', () => {
    it('should save configuration with teams and room models', async () => {
      const mockConfig: Config = {
        agents: {
          agent1: {
            display_name: 'Agent 1',
            role: 'Test agent',
            tools: [],
            instructions: [],
            rooms: [],
            num_history_runs: 5,
          },
        },
        teams: {
          team1: {
            display_name: 'Team 1',
            role: 'Test team',
            agents: ['agent1'],
            rooms: ['lobby'],
            mode: 'coordinate',
          },
        },
        room_models: {
          lobby: 'default',
        },
        memory: {
          embedder: {
            provider: 'ollama',
            config: {
              model: 'nomic-embed-text',
              host: 'http://localhost:11434',
            },
          },
        },
        models: {
          default: {
            provider: 'ollama',
            id: 'test-model',
          },
        },
        defaults: {
          num_history_runs: 5,
          markdown: true,
          add_history_to_messages: true,
        },
        router: {
          model: 'default',
        },
      };

      useConfigStore.setState({
        config: mockConfig,
        agents: [
          {
            id: 'agent1',
            display_name: 'Agent 1',
            role: 'Test agent',
            tools: [],
            instructions: [],
            rooms: [],
            num_history_runs: 5,
          },
        ],
        teams: [
          {
            id: 'team1',
            display_name: 'Team 1',
            role: 'Test team',
            agents: ['agent1'],
            rooms: ['lobby'],
            mode: 'coordinate',
          },
        ],
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({}),
      });

      const { saveConfig } = useConfigStore.getState();
      await saveConfig();

      expect(global.fetch).toHaveBeenCalledWith('/api/config/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(mockConfig),
      });

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe('synced');
      expect(state.isDirty).toBe(false);
    });
  });
});
