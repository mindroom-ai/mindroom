import { describe, it, expect, beforeEach, vi } from 'vitest'
import { useConfigStore } from './configStore'
import type { Agent, Model } from '@/types/config'

// Mock fetch globally
global.fetch = vi.fn()

describe('configStore', () => {
  beforeEach(() => {
    // Reset store state
    useConfigStore.setState({
      config: null,
      agents: [],
      selectedAgentId: null,
      isDirty: false,
      syncStatus: 'disconnected',
    })

    // Clear all mocks
    vi.clearAllMocks()
  })

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
      }

      ;(global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => mockConfig,
      })

      const { loadConfig } = useConfigStore.getState()
      await loadConfig()

      const state = useConfigStore.getState()
      expect(state.config).toEqual(mockConfig)
      expect(state.agents).toHaveLength(1)
      expect(state.agents[0].id).toBe('test')
      expect(state.agents[0].display_name).toBe('Test Agent')
      expect(state.syncStatus).toBe('synced')
    })

    it('should handle load errors', async () => {
      ;(global.fetch as any).mockRejectedValueOnce(new Error('Network error'))

      const { loadConfig } = useConfigStore.getState()
      await loadConfig()

      const state = useConfigStore.getState()
      expect(state.syncStatus).toBe('error')
    })
  })

  describe('saveConfig', () => {
    it('should save configuration successfully', async () => {
      // Set up initial state with agents array
      const mockConfig = {
        agents: { test: { display_name: 'Test' } },
        models: {},
      }
      const mockAgents = [{
        id: 'test',
        display_name: 'Test',
        role: 'Test role',
        tools: [],
        instructions: [],
        rooms: [],
        num_history_runs: 5,
      }]
      useConfigStore.setState({
        config: mockConfig,
        agents: mockAgents,
        isDirty: true,
        syncStatus: 'synced',
      })

      ;(global.fetch as any).mockResolvedValueOnce({
        ok: true,
        json: async () => ({ success: true }),
      })

      const { saveConfig } = useConfigStore.getState()
      await saveConfig()

      // The saveConfig removes the id field when saving
      const { id, ...agentWithoutId } = mockAgents[0]
      expect(global.fetch).toHaveBeenCalledWith('/api/config/save', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agents: { test: agentWithoutId },
          models: {},
        }),
      })

      const state = useConfigStore.getState()
      expect(state.isDirty).toBe(false)
      expect(state.syncStatus).toBe('synced')
    })
  })

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
      ]
      useConfigStore.setState({ agents })
    })

    it('should select agent', () => {
      const { selectAgent } = useConfigStore.getState()
      selectAgent('agent2')

      const state = useConfigStore.getState()
      expect(state.selectedAgentId).toBe('agent2')
    })

    it('should update agent', () => {
      const { updateAgent } = useConfigStore.getState()
      updateAgent('agent1', { display_name: 'Updated Agent' })

      const state = useConfigStore.getState()
      const updatedAgent = state.agents.find(a => a.id === 'agent1')
      expect(updatedAgent?.display_name).toBe('Updated Agent')
      expect(state.isDirty).toBe(true)
    })

    it('should create new agent', () => {
      const newAgentData = {
        display_name: 'New Agent',
        role: 'New role',
        tools: [],
        instructions: [],
        rooms: [],
        num_history_runs: 5,
      }

      const { createAgent } = useConfigStore.getState()
      createAgent(newAgentData)

      const state = useConfigStore.getState()
      expect(state.agents).toHaveLength(3)
      const newAgent = state.agents[2]
      expect(newAgent.display_name).toBe('New Agent')
      expect(state.selectedAgentId).toBe(newAgent.id)
      expect(state.isDirty).toBe(true)
    })

    it('should delete agent', () => {
      const { deleteAgent } = useConfigStore.getState()
      deleteAgent('agent1')

      const state = useConfigStore.getState()
      expect(state.agents).toHaveLength(1)
      expect(state.agents[0].id).toBe('agent2')
      expect(state.isDirty).toBe(true)
    })
  })

  describe('dirty state', () => {
    it('should mark state as dirty', () => {
      const { markDirty } = useConfigStore.getState()
      markDirty()

      const state = useConfigStore.getState()
      expect(state.isDirty).toBe(true)
    })
  })
})
