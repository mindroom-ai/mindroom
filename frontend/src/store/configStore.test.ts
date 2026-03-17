import { describe, it, expect, beforeEach, vi } from 'vitest';
import { waitFor } from '@testing-library/react';
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
      teams: [],
      cultures: [],
      rooms: [],
      teamEligibilityByAgent: {},
      teamEligibilityRequestId: 0,
      selectedAgentId: null,
      selectedTeamId: null,
      selectedCultureId: null,
      selectedRoomId: null,
      isDirty: false,
      isLoading: false,
      error: null,
      editorError: null,
      configValidationIssues: [],
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
            skills: [],
            instructions: ['Test instruction'],
            rooms: ['lobby'],
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ team_eligibility: { test: null } }),
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.config).toEqual({ ...mockConfig, knowledge_bases: {}, cultures: {} });
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe('test');
      expect(state.agents[0].display_name).toBe('Test Agent');
      expect(state.agents[0].learning).toBe(true);
      expect(state.agents[0].learning_mode).toBe('always');
      expect(state.teamEligibilityByAgent).toEqual({ test: null });
      expect(state.syncStatus).toBe('synced');
    });

    it('should apply global learning defaults when agent settings are omitted', async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: 'Test Agent',
            role: 'Test role',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        },
        defaults: {
          learning: false,
          learning_mode: 'agentic',
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ team_eligibility: { test: null } }),
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.agents[0].learning).toBe(false);
      expect(state.agents[0].learning_mode).toBe('agentic');
    });

    it('should preserve explicit learning=false from configuration', async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: 'Test Agent',
            role: 'Test role',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            learning: false,
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ team_eligibility: { test: null } }),
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.agents[0].learning).toBe(false);
      expect(state.agents[0].learning_mode).toBe('always');
    });

    it('should preserve explicit learning_mode from configuration', async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: 'Test Agent',
            role: 'Test role',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            learning: true,
            learning_mode: 'agentic',
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ team_eligibility: { test: null } }),
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.agents[0].learning_mode).toBe('agentic');
    });

    it('should preserve private agent configuration from the backend', async () => {
      const mockConfig = {
        agents: {
          mind: {
            display_name: 'Mind',
            role: 'Private assistant',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: {
              per: 'user',
              root: 'mind_data',
              context_files: ['SOUL.md'],
              knowledge: {
                enabled: true,
                path: 'memory',
                watch: true,
              },
            },
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: { mind: 'Private agents cannot participate in teams yet.' },
        }),
      });

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.agents[0].private).toEqual({
        per: 'user',
        root: 'mind_data',
        context_files: ['SOUL.md'],
        knowledge: {
          enabled: true,
          path: 'memory',
          watch: true,
        },
      });
    });

    it('should handle load errors', async () => {
      (global.fetch as any).mockRejectedValueOnce(new Error('Network error'));

      const { loadConfig } = useConfigStore.getState();
      await loadConfig();

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe('error');
    });

    it('keeps the loaded config when team eligibility derivation fails', async () => {
      const mockConfig = {
        agents: {
          test: {
            display_name: 'Test Agent',
            role: 'Test role',
            tools: ['calculator'],
            skills: [],
            instructions: ['Test instruction'],
            rooms: ['lobby'],
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
      (global.fetch as any).mockResolvedValueOnce({
        ok: false,
        status: 500,
        json: async () => ({ detail: 'boom' }),
      });

      await useConfigStore.getState().loadConfig();

      const state = useConfigStore.getState();
      expect(state.config).toEqual({ ...mockConfig, knowledge_bases: {}, cultures: {} });
      expect(state.teams).toEqual([]);
      expect(state.teamEligibilityByAgent).toEqual({});
      expect(state.editorError).toBe('Failed to derive team eligibility');
      expect(state.error).toBeNull();
      expect(state.syncStatus).toBe('synced');
    });
  });

  describe('refreshTeamEligibility', () => {
    it('stores backend-derived eligibility reasons', async () => {
      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: {
            helper: null,
            mind: 'Private agents cannot participate in teams yet.',
          },
        }),
      });

      await useConfigStore.getState().refreshTeamEligibility([
        {
          id: 'helper',
          display_name: 'Helper',
          role: 'Helps',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: 'mind',
          display_name: 'Mind',
          role: 'Private',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
          private: { per: 'user' },
        },
      ]);

      expect(useConfigStore.getState().teamEligibilityByAgent).toEqual({
        helper: null,
        mind: 'Private agents cannot participate in teams yet.',
      });
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
            skills: [],
            instructions: [],
            rooms: [],
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
          markdown: true,
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
          skills: [],
          instructions: [],
          rooms: [],
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
      const { id: _id, ...agentWithoutId } = mockAgents[0];
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenNthCalledWith(1, '/api/config/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...mockConfig,
          agents: { test: agentWithoutId },
          teams: {}, // saveConfig adds empty teams if not present
          cultures: {}, // saveConfig adds empty cultures if not present
        }),
      });

      const state = useConfigStore.getState();
      expect(state.isDirty).toBe(false);
      expect(state.syncStatus).toBe('synced');
    });

    it('stores backend validation issues without poisoning the global load error', async () => {
      const mockConfig: Config = {
        models: {
          default: { provider: 'test', id: 'test-model' },
        },
        memory: {
          embedder: {
            provider: 'openai',
            config: {
              model: 'text-embedding-ada-002',
            },
          },
        },
        agents: {},
        defaults: {
          markdown: true,
        },
        router: {
          model: 'default',
        },
      };
      const mockAgents = [
        {
          id: 'mind',
          display_name: 'Mind',
          role: 'Assistant',
          tools: [],
          skills: [],
          instructions: [],
          rooms: [],
          private: {
            per: 'user' as const,
            root: '../outside',
          },
        },
      ];
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        isDirty: true,
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: false,
        status: 422,
        json: async () => ({
          detail: [
            {
              loc: ['agents', 'mind', 'private', 'root'],
              msg: 'private.root must stay within the private instance root',
              type: 'value_error',
            },
          ],
        }),
      });

      await useConfigStore.getState().saveConfig();

      const state = useConfigStore.getState();
      expect(state.error).toBeNull();
      expect(state.editorError).toBeNull();
      expect(state.configValidationIssues).toEqual([
        {
          loc: ['agents', 'mind', 'private', 'root'],
          msg: 'private.root must stay within the private instance root',
          type: 'value_error',
        },
      ]);
    });
  });

  describe('updateAgent', () => {
    it('clears legacy worker_scope when private state is enabled', () => {
      useConfigStore.setState({
        agents: [
          {
            id: 'mind',
            display_name: 'Mind',
            role: 'Assistant',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            worker_scope: 'user_agent',
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().updateAgent('mind', {
        private: { per: 'user_agent' },
      });

      const state = useConfigStore.getState();
      expect(state.agents[0].worker_scope).toBeUndefined();
      expect(state.agents[0].private).toEqual({ per: 'user_agent' });
    });

    it('defaults private knowledge path when enabling it from an empty state', () => {
      useConfigStore.setState({
        agents: [
          {
            id: 'mind',
            display_name: 'Mind',
            role: 'Assistant',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        isDirty: false,
      });

      useConfigStore.getState().updateAgent('mind', {
        private: {
          per: 'user',
          knowledge: { enabled: true, watch: true },
        },
      });

      const state = useConfigStore.getState();
      expect(state.agents[0].private?.knowledge).toEqual({
        enabled: true,
        path: 'memory',
        watch: true,
      });
    });

    it('keeps draft team membership when backend eligibility marks an edited agent unsupported', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'leader',
            display_name: 'Leader',
            role: 'Lead',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: 'helper',
            display_name: 'Helper',
            role: 'Help',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
        teams: [
          {
            id: 'duo',
            display_name: 'Duo',
            role: 'Two agents',
            agents: ['leader', 'helper'],
            rooms: [],
            mode: 'coordinate',
          },
        ],
        teamEligibilityByAgent: { leader: null, helper: null },
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: {
            leader: 'Private agents cannot participate in teams yet.',
            helper: null,
          },
        }),
      });

      useConfigStore.getState().updateAgent('leader', {
        private: { per: 'user' },
      });

      await waitFor(() => {
        expect(useConfigStore.getState().teams[0].agents).toEqual(['leader', 'helper']);
        expect(useConfigStore.getState().teamEligibilityByAgent).toEqual({
          leader: 'Private agents cannot participate in teams yet.',
          helper: null,
        });
      });
    });

    it('does not refresh team eligibility for non-policy agent edits', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'leader',
            display_name: 'Leader',
            role: 'Lead',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: [],
          },
        ],
      });

      useConfigStore.getState().updateAgent('leader', {
        display_name: 'Updated Leader',
      });

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
      expect(useConfigStore.getState().agents[0].display_name).toBe('Updated Leader');
    });

    it('does not refresh team eligibility for private workspace edits that keep private mode enabled', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'mind',
            display_name: 'Mind',
            role: 'Private',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: {
              per: 'user',
              root: 'mind_data',
            },
          },
        ],
      });

      useConfigStore.getState().updateAgent('mind', {
        private: {
          per: 'user',
          root: 'updated_root',
        },
      });

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
      expect(useConfigStore.getState().agents[0].private?.root).toBe('updated_root');
    });

    it('keeps draft team membership when delegation now reaches a private agent', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          agents: {},
          defaults: { markdown: true },
          models: {},
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'leader',
            display_name: 'Leader',
            role: 'Lead',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: [],
          },
          {
            id: 'helper',
            display_name: 'Helper',
            role: 'Help',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: 'mind',
            display_name: 'Mind',
            role: 'Private',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: { per: 'user' },
          },
        ],
        teams: [
          {
            id: 'duo',
            display_name: 'Duo',
            role: 'Two agents',
            agents: ['leader', 'helper'],
            rooms: [],
            mode: 'coordinate',
          },
        ],
        teamEligibilityByAgent: {
          leader: null,
          helper: null,
          mind: 'Private agents cannot participate in teams yet.',
        },
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: {
            leader: "Delegates to private agent 'mind', so it cannot participate in teams yet.",
            helper: null,
            mind: 'Private agents cannot participate in teams yet.',
          },
        }),
      });

      useConfigStore.getState().updateAgent('leader', {
        delegate_to: ['mind'],
      });

      await waitFor(() => {
        expect(useConfigStore.getState().teams[0].agents).toEqual(['leader', 'helper']);
        expect(useConfigStore.getState().teamEligibilityByAgent).toEqual({
          leader: "Delegates to private agent 'mind', so it cannot participate in teams yet.",
          helper: null,
          mind: 'Private agents cannot participate in teams yet.',
        });
      });
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
          skills: [],
          instructions: [],
          rooms: [],
        },
        {
          id: 'agent2',
          display_name: 'Agent 2',
          role: 'Role 2',
          tools: ['calculator'],
          skills: [],
          instructions: ['Test'],
          rooms: ['lobby'],
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
        skills: [],
        instructions: [],
        rooms: [],
      };

      const { createAgent } = useConfigStore.getState();
      createAgent(newAgentData);

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(3);
      const newAgent = state.agents[2];
      expect(newAgent.display_name).toBe('New Agent');
      expect(newAgent.learning).toBe(true);
      expect(newAgent.learning_mode).toBe('always');
      expect(state.selectedAgentId).toBe(newAgent.id);
      expect(state.isDirty).toBe(true);
    });

    it('should create new agent with learning values from global defaults', () => {
      const newAgentData = {
        display_name: 'New Agent',
        role: 'New role',
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      };
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
            learning: false,
            learning_mode: 'agentic',
          },
          router: { model: 'default' },
        },
      });

      const { createAgent } = useConfigStore.getState();
      createAgent(newAgentData);

      const state = useConfigStore.getState();
      const newAgent = state.agents[state.agents.length - 1];
      expect(newAgent?.learning).toBe(false);
      expect(newAgent?.learning_mode).toBe('agentic');
    });

    it('does not refresh team eligibility when creating a standard shared agent', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        },
      });

      useConfigStore.getState().createAgent({
        display_name: 'New Agent',
        role: 'New role',
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
      });

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
    });

    it('refreshes team eligibility when creating a private agent draft', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        },
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: {
            new_agent: 'Private agents cannot participate in teams yet.',
          },
        }),
      });

      useConfigStore.getState().createAgent({
        display_name: 'New Agent',
        role: 'New role',
        tools: [],
        skills: [],
        instructions: [],
        rooms: [],
        private: { per: 'user' },
      });

      await waitFor(() => {
        expect(useConfigStore.getState().teamEligibilityByAgent).toEqual({
          new_agent: 'Private agents cannot participate in teams yet.',
        });
      });
    });

    it('should delete agent', () => {
      useConfigStore.setState({
        cultures: [
          {
            id: 'engineering',
            description: 'Engineering standards',
            agents: ['agent1', 'agent2'],
            mode: 'automatic',
          },
        ],
        teams: [
          {
            id: 'team1',
            display_name: 'Team 1',
            role: 'Test team',
            agents: ['agent1', 'agent2'],
            rooms: [],
            mode: 'coordinate',
          },
        ],
      });
      const { deleteAgent } = useConfigStore.getState();
      deleteAgent('agent1');

      const state = useConfigStore.getState();
      expect(state.agents).toHaveLength(1);
      expect(state.agents[0].id).toBe('agent2');
      expect(state.cultures[0].agents).toEqual(['agent2']);
      expect(state.teams[0].agents).toEqual(['agent2']);
      expect(state.isDirty).toBe(true);
    });

    it('does not refresh team eligibility when deleting an unrelated shared agent', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'agent1',
            display_name: 'Agent 1',
            role: 'Role 1',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
          {
            id: 'agent2',
            display_name: 'Agent 2',
            role: 'Role 2',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
          },
        ],
      });

      useConfigStore.getState().deleteAgent('agent1');

      await Promise.resolve();

      expect(global.fetch).not.toHaveBeenCalled();
    });

    it('refreshes team eligibility when deleting an agent referenced by delegation', async () => {
      useConfigStore.setState({
        config: {
          memory: {
            embedder: {
              provider: 'openai',
              config: { model: 'text-embedding-3-small' },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'leader',
            display_name: 'Leader',
            role: 'Lead',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            delegate_to: ['mind'],
          },
          {
            id: 'mind',
            display_name: 'Mind',
            role: 'Private',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            private: { per: 'user' },
          },
        ],
        teamEligibilityByAgent: {
          leader: "Delegates to private agent 'mind', so it cannot participate in teams yet.",
          mind: 'Private agents cannot participate in teams yet.',
        },
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          team_eligibility: {
            leader: null,
          },
        }),
      });

      useConfigStore.getState().deleteAgent('mind');

      await waitFor(() => {
        expect(useConfigStore.getState().teamEligibilityByAgent).toEqual({
          leader: null,
        });
      });
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

  describe('cultures', () => {
    beforeEach(() => {
      useConfigStore.setState({
        cultures: [
          {
            id: 'engineering',
            description: 'Engineering standards',
            agents: ['agent1'],
            mode: 'automatic',
          },
          {
            id: 'support',
            description: 'Support playbooks',
            agents: ['agent2'],
            mode: 'manual',
          },
        ],
        selectedCultureId: 'engineering',
      });
    });

    it('should select culture', () => {
      const { selectCulture } = useConfigStore.getState();
      selectCulture('support');

      const state = useConfigStore.getState();
      expect(state.selectedCultureId).toBe('support');
    });

    it('should update culture and enforce unique agent assignment', () => {
      const { updateCulture } = useConfigStore.getState();
      updateCulture('support', { agents: ['agent1', 'agent2'], mode: 'agentic' });

      const state = useConfigStore.getState();
      expect(state.cultures.find(culture => culture.id === 'support')?.mode).toBe('agentic');
      expect(state.cultures.find(culture => culture.id === 'support')?.agents).toEqual([
        'agent1',
        'agent2',
      ]);
      expect(state.cultures.find(culture => culture.id === 'engineering')?.agents).toEqual([]);
      expect(state.isDirty).toBe(true);
    });

    it('should create new culture', () => {
      const { createCulture } = useConfigStore.getState();
      createCulture({
        description: 'Product knowledge',
        agents: ['agent3'],
        mode: 'automatic',
      });

      const state = useConfigStore.getState();
      expect(state.cultures).toHaveLength(3);
      const newCulture = state.cultures.find(culture => culture.id === 'product_knowledge');
      expect(newCulture?.description).toBe('Product knowledge');
      expect(state.selectedCultureId).toBe('product_knowledge');
      expect(state.isDirty).toBe(true);
    });

    it('should delete culture', () => {
      const { deleteCulture } = useConfigStore.getState();
      deleteCulture('engineering');

      const state = useConfigStore.getState();
      expect(state.cultures).toHaveLength(1);
      expect(state.cultures[0].id).toBe('support');
      expect(state.selectedCultureId).toBe(null);
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
            markdown: true,
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
            markdown: true,
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
            markdown: true,
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

  describe('knowledge bases', () => {
    it('should preserve git settings when updating base path or watch', () => {
      useConfigStore.setState({
        config: {
          memory: { embedder: { provider: 'openai', config: { model: 'test' } } },
          knowledge_bases: {
            docs: {
              path: './docs',
              watch: true,
              chunk_size: 5000,
              chunk_overlap: 0,
              git: {
                repo_url: 'https://github.com/pipefunc/pipefunc',
                branch: 'main',
                include_patterns: ['docs/**'],
              },
            },
          },
          models: {},
          agents: {},
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        } as Config,
      });

      const { updateKnowledgeBase } = useConfigStore.getState();
      updateKnowledgeBase('docs', { path: './docs-sync', watch: false });

      const state = useConfigStore.getState();
      expect(state.config?.knowledge_bases?.docs).toEqual({
        path: './docs-sync',
        watch: false,
        chunk_size: 5000,
        chunk_overlap: 0,
        git: {
          repo_url: 'https://github.com/pipefunc/pipefunc',
          branch: 'main',
          include_patterns: ['docs/**'],
        },
      });
      expect(state.isDirty).toBe(true);
    });

    it('should remove deleted knowledge base from all agent assignments', () => {
      useConfigStore.setState({
        config: {
          memory: { embedder: { provider: 'openai', config: { model: 'test' } } },
          knowledge_bases: {
            legal: { path: './legal', watch: true },
            research: { path: './research', watch: true },
          },
          models: {},
          agents: {
            agent1: {
              display_name: 'Agent 1',
              role: 'Test agent',
              tools: [],
              skills: [],
              instructions: [],
              rooms: [],
              knowledge_bases: ['research', 'legal'],
            },
            agent2: {
              display_name: 'Agent 2',
              role: 'Test agent 2',
              tools: [],
              skills: [],
              instructions: [],
              rooms: [],
              knowledge_bases: ['research'],
            },
          },
          defaults: {
            markdown: true,
          },
          router: { model: 'default' },
        },
        agents: [
          {
            id: 'agent1',
            display_name: 'Agent 1',
            role: 'Test agent',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            knowledge_bases: ['research', 'legal'],
          },
          {
            id: 'agent2',
            display_name: 'Agent 2',
            role: 'Test agent 2',
            tools: [],
            skills: [],
            instructions: [],
            rooms: [],
            knowledge_bases: ['research'],
          },
        ],
      });

      const { deleteKnowledgeBase } = useConfigStore.getState();
      deleteKnowledgeBase('research');

      const state = useConfigStore.getState();
      expect(state.config?.knowledge_bases).toEqual({
        legal: { path: './legal', watch: true },
      });
      expect(state.agents.find(agent => agent.id === 'agent1')?.knowledge_bases).toEqual(['legal']);
      expect(state.agents.find(agent => agent.id === 'agent2')?.knowledge_bases).toEqual([]);
      expect(state.config?.agents.agent1.knowledge_bases).toEqual(['legal']);
      expect(state.config?.agents.agent2.knowledge_bases).toEqual([]);
      expect(state.isDirty).toBe(true);
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
          skills: [],
          instructions: [],
          rooms: ['lobby'],
        },
        {
          id: 'agent2',
          display_name: 'Agent 2',
          role: 'Test agent 2',
          tools: [],
          skills: [],
          instructions: [],
          rooms: ['dev'],
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
            skills: [],
            instructions: [],
            rooms: [],
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
          markdown: true,
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
            skills: [],
            instructions: [],
            rooms: [],
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
        rooms: [
          {
            id: 'lobby',
            display_name: 'Lobby',
            description: '',
            agents: ['agent1'],
            model: 'default',
          },
        ],
      });

      (global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({}),
      });

      const { saveConfig } = useConfigStore.getState();
      await saveConfig();

      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(global.fetch).toHaveBeenNthCalledWith(1, '/api/config/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...mockConfig, cultures: {} }),
      });

      const state = useConfigStore.getState();
      expect(state.syncStatus).toBe('synced');
      expect(state.isDirty).toBe(false);
    });
  });
});
