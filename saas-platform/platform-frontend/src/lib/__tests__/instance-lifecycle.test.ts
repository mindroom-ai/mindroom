/**
 * Tests for critical instance lifecycle operations
 * This ensures we're testing the real behaviors users care about:
 * - Provisioning new instances
 * - Starting/stopping instances
 * - Restarting instances
 * - Deprovisioning/uninstalling instances
 * - Error recovery and retries
 */

import {
  provisionInstance,
  startInstance,
  stopInstance,
  restartInstance,
  apiCall
} from '../api'
import { createClient } from '../supabase/client'

jest.mock('../supabase/client', () => ({
  createClient: jest.fn()
}))

describe('Instance Lifecycle Operations', () => {
  let mockSupabase: any
  let mockFetch: jest.MockedFunction<typeof fetch>

  beforeEach(() => {
    jest.clearAllMocks()

    // Setup mock Supabase client
    mockSupabase = {
      auth: {
        getSession: jest.fn().mockResolvedValue({
          data: {
            session: {
              access_token: 'test-token-123',
              user: { id: 'user-123' }
            }
          }
        })
      }
    }
    ;(createClient as jest.Mock).mockReturnValue(mockSupabase)

    mockFetch = global.fetch as jest.MockedFunction<typeof fetch>
    mockFetch.mockClear()
  })

  describe('Instance Provisioning', () => {
    it('should handle complete provisioning flow', async () => {
      // Step 1: User provisions instance
      const provisionResponse = {
        instance_id: 1,
        status: 'provisioning',
        message: 'Instance is being provisioned'
      }
      mockFetch.mockResolvedValueOnce(
        new Response(JSON.stringify(provisionResponse), { status: 200 })
      )

      const result = await provisionInstance()

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:8000/my/instances/provision',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({
            'Authorization': 'Bearer test-token-123'
          })
        })
      )
      expect(result).toEqual(provisionResponse)
    })

    it('should handle quota exceeded during provisioning', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Instance quota exceeded for account', { status: 403 })
      )

      await expect(provisionInstance()).rejects.toThrow('Instance quota exceeded for account')
    })

    it('should handle subscription issues during provisioning', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('No active subscription found', { status: 402 })
      )

      await expect(provisionInstance()).rejects.toThrow('No active subscription found')
    })

    it('should handle concurrent provisioning attempts', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Instance already being provisioned', { status: 409 })
      )

      await expect(provisionInstance()).rejects.toThrow('Instance already being provisioned')
    })
  })

  describe('Instance State Management', () => {
    describe('Starting Instances', () => {
      it('should start a stopped instance', async () => {
        const response = { success: true, status: 'running' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await startInstance(1)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances/1/start',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(response)
      })

      it('should handle starting already running instance', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Instance is already running', { status: 400 })
        )

        await expect(startInstance(1)).rejects.toThrow('Instance is already running')
      })

      it('should handle starting deprovisioned instance', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Cannot start deprovisioned instance', { status: 400 })
        )

        await expect(startInstance(1)).rejects.toThrow('Cannot start deprovisioned instance')
      })
    })

    describe('Stopping Instances', () => {
      it('should stop a running instance', async () => {
        const response = { success: true, status: 'stopped' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await stopInstance(1)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances/1/stop',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(response)
      })

      it('should handle stopping already stopped instance', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Instance is already stopped', { status: 400 })
        )

        await expect(stopInstance(1)).rejects.toThrow('Instance is already stopped')
      })
    })

    describe('Restarting Instances', () => {
      it('should restart a running instance', async () => {
        const response = { success: true, status: 'restarting' }
        mockFetch.mockResolvedValueOnce(
          new Response(JSON.stringify(response), { status: 200 })
        )

        const result = await restartInstance(1)

        expect(mockFetch).toHaveBeenCalledWith(
          'http://localhost:8000/my/instances/1/restart',
          expect.objectContaining({ method: 'POST' })
        )
        expect(result).toEqual(response)
      })

      it('should handle restart during maintenance', async () => {
        mockFetch.mockResolvedValueOnce(
          new Response('Instance is under maintenance', { status: 503 })
        )

        await expect(restartInstance(1)).rejects.toThrow('Instance is under maintenance')
      })
    })
  })

  describe('Instance Deprovisioning/Uninstall', () => {
    it('should deprovision/uninstall an instance', async () => {
      const response = { success: true, status: 'deprovisioned' }
      mockFetch.mockResolvedValueOnce(
        new Response(JSON.stringify(response), { status: 200 })
      )

      const result = await apiCall('/admin/instances/1/uninstall', { method: 'DELETE' })
      const data = await result.json()

      expect(mockFetch).toHaveBeenCalledWith(
        'http://localhost:8000/admin/instances/1/uninstall',
        expect.objectContaining({ method: 'DELETE' })
      )
      expect(data).toEqual(response)
    })

    it('should handle uninstall of running instance with confirmation', async () => {
      // First stop the instance
      mockFetch.mockResolvedValueOnce(
        new Response('{}', { status: 200 })
      )
      await stopInstance(1)

      // Then uninstall
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "deprovisioned"}', { status: 200 })
      )
      const result = await apiCall('/admin/instances/1/uninstall', { method: 'DELETE' })

      expect(result.ok).toBe(true)
    })

    it('should prevent uninstall of instance with active connections', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Cannot uninstall: instance has active connections', { status: 409 })
      )

      const response = await apiCall('/admin/instances/1/uninstall', { method: 'DELETE' })

      expect(response.ok).toBe(false)
      expect(await response.text()).toBe('Cannot uninstall: instance has active connections')
    })

    it('should handle uninstall rollback on failure', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Uninstall failed: rollback initiated', { status: 500 })
      )

      const response = await apiCall('/admin/instances/1/uninstall', { method: 'DELETE' })

      expect(response.ok).toBe(false)
      expect(await response.text()).toBe('Uninstall failed: rollback initiated')
    })
  })

  describe('Error Recovery and Retries', () => {
    it('should handle instance in error state recovery', async () => {
      // Instance is in error state
      mockFetch.mockResolvedValueOnce(
        new Response('Instance in error state', { status: 500 })
      )
      await expect(startInstance(1)).rejects.toThrow('Instance in error state')

      // Admin fixes the instance
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "stopped"}', { status: 200 })
      )
      const fixResponse = await apiCall('/admin/instances/1/fix', { method: 'POST' })
      expect(fixResponse.ok).toBe(true)

      // Now can start the instance
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "running"}', { status: 200 })
      )
      const result = await startInstance(1)
      expect(result.status).toBe('running')
    })

    it('should handle provisioning retry after failure', async () => {
      // First attempt fails
      mockFetch.mockResolvedValueOnce(
        new Response('Temporary provisioning error', { status: 503 })
      )
      await expect(provisionInstance()).rejects.toThrow('Temporary provisioning error')

      // Retry succeeds
      mockFetch.mockResolvedValueOnce(
        new Response('{"instance_id": 2, "status": "provisioning"}', { status: 200 })
      )
      const result = await provisionInstance()
      expect(result.instance_id).toBe(2)
    })

    it('should handle network failures during operations', async () => {
      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()

      // Network failure
      mockFetch.mockRejectedValueOnce(new TypeError('Network request failed'))
      await expect(startInstance(1)).rejects.toThrow('Network request failed')

      // Retry after network recovery
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "running"}', { status: 200 })
      )
      const result = await startInstance(1)
      expect(result.status).toBe('running')

      consoleSpy.mockRestore()
    })
  })

  describe('Instance Lifecycle State Transitions', () => {
    it('should handle complete lifecycle: provision -> start -> stop -> restart -> uninstall', async () => {
      // 1. Provision
      mockFetch.mockResolvedValueOnce(
        new Response('{"instance_id": 1, "status": "provisioning"}', { status: 200 })
      )
      const provision = await provisionInstance()
      expect(provision.status).toBe('provisioning')

      // 2. Start (after provisioning completes)
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "running"}', { status: 200 })
      )
      const start = await startInstance(1)
      expect(start.status).toBe('running')

      // 3. Stop
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "stopped"}', { status: 200 })
      )
      const stop = await stopInstance(1)
      expect(stop.status).toBe('stopped')

      // 4. Restart
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "running"}', { status: 200 })
      )
      const restart = await restartInstance(1)
      expect(restart.status).toBe('running')

      // 5. Uninstall/Deprovision
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "deprovisioned"}', { status: 200 })
      )
      const uninstall = await apiCall('/admin/instances/1/uninstall', { method: 'DELETE' })
      const uninstallData = await uninstall.json()
      expect(uninstallData.status).toBe('deprovisioned')
    })

    it('should validate invalid state transitions', async () => {
      // Cannot restart a stopped instance
      mockFetch.mockResolvedValueOnce(
        new Response('Cannot restart stopped instance', { status: 400 })
      )
      await expect(restartInstance(1)).rejects.toThrow('Cannot restart stopped instance')

      // Cannot stop a provisioning instance
      mockFetch.mockResolvedValueOnce(
        new Response('Cannot stop instance while provisioning', { status: 400 })
      )
      await expect(stopInstance(1)).rejects.toThrow('Cannot stop instance while provisioning')
    })
  })

  describe('Billing and Resource Management', () => {
    it('should track billing during instance lifecycle', async () => {
      // Start instance - billing starts
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "running", "billing_started": true}', { status: 200 })
      )
      const start = await startInstance(1)
      expect(start.billing_started).toBe(true)

      // Stop instance - billing pauses
      mockFetch.mockResolvedValueOnce(
        new Response('{"status": "stopped", "billing_paused": true}', { status: 200 })
      )
      const stop = await stopInstance(1)
      expect(stop.billing_paused).toBe(true)
    })

    it('should enforce resource limits', async () => {
      mockFetch.mockResolvedValueOnce(
        new Response('Resource limit exceeded: max instances reached', { status: 429 })
      )
      await expect(provisionInstance()).rejects.toThrow('Resource limit exceeded')
    })
  })
})
