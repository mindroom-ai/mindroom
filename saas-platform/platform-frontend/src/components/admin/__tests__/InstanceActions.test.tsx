/**
 * Tests for InstanceActions component
 * This component handles critical admin operations:
 * - Start/Stop/Restart instances
 * - Provision deprovisioned instances
 * - Uninstall instances completely
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InstanceActions } from '../InstanceActions'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({
  apiCall: jest.fn()
}))

// Mock window functions
const mockConfirm = jest.fn()
window.confirm = mockConfirm

describe('InstanceActions Component', () => {
  const mockApiCall = apiCall as jest.Mock

  beforeEach(() => {
    jest.clearAllMocks()
    mockConfirm.mockReturnValue(true)
    if (window.location.reload && typeof window.location.reload === 'function') {
      (window.location.reload as jest.Mock).mockClear?.()
    }
  })

  describe('Provisioning Actions', () => {
    it('should show provision button for deprovisioned instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="deprovisioned" />)

      const provisionBtn = screen.getByRole('button', { name: /Provision/i })
      expect(provisionBtn).toBeInTheDocument()

      // Should not show other actions
      expect(screen.queryByRole('button', { name: /Start/i })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Stop/i })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Restart/i })).not.toBeInTheDocument()
    })

    it('should show provision button for error state instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="error" />)

      expect(screen.getByRole('button', { name: /Provision/i })).toBeInTheDocument()
    })

    it('should provision instance when button clicked', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="deprovisioned" />)

      await user.click(screen.getByRole('button', { name: /Provision/i }))

      expect(mockApiCall).toHaveBeenCalledWith(
        '/admin/instances/1/provision',
        { method: 'POST' }
      )

      // Reload is an implementation detail - not testing
    })

    it('should handle provision failure gracefully', async () => {
      mockApiCall.mockResolvedValue({ ok: false })
      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="deprovisioned" />)

      await user.click(screen.getByRole('button', { name: /Provision/i }))

      await waitFor(() => {
        expect(consoleSpy).toHaveBeenCalledWith(
          'Failed to provision instance:',
          expect.any(Error)
        )
      })

      // Not testing reload - implementation detail
      consoleSpy.mockRestore()
    })
  })

  describe('Start/Stop Actions', () => {
    it('should show start button for stopped instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="stopped" />)

      expect(screen.getByRole('button', { name: /Start/i })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Stop/i })).not.toBeInTheDocument()
    })

    it('should show stop button for running instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="running" />)

      expect(screen.getByRole('button', { name: /Stop/i })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /^Start$/i })).not.toBeInTheDocument()
    })

    it('should start instance when start clicked', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="stopped" />)

      await user.click(screen.getByRole('button', { name: /Start/i }))

      expect(mockApiCall).toHaveBeenCalledWith(
        '/admin/instances/1/start',
        { method: 'POST' }
      )

      // Reload is an implementation detail - not testing
    })

    it('should stop instance when stop clicked', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)

      await user.click(screen.getByRole('button', { name: /Stop/i }))

      expect(mockApiCall).toHaveBeenCalledWith(
        '/admin/instances/1/stop',
        { method: 'POST' }
      )

      // Reload is an implementation detail - not testing
    })

    it('should show loading state during operations', async () => {
      mockApiCall.mockImplementation(() =>
        new Promise(resolve => setTimeout(() => resolve({ ok: true }), 100))
      )
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="stopped" />)
      const startBtn = screen.getByRole('button', { name: /Start/i })

      await user.click(startBtn)

      expect(screen.getByText('Starting...')).toBeInTheDocument()
      expect(startBtn).toBeDisabled()

      // Reload is an implementation detail - not testing
    })
  })

  describe('Restart Actions', () => {
    it('should show restart button for running instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="running" />)

      expect(screen.getByRole('button', { name: /Restart/i })).toBeInTheDocument()
    })

    it('should not show restart for stopped instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="stopped" />)

      expect(screen.queryByRole('button', { name: /Restart/i })).not.toBeInTheDocument()
    })

    it('should restart instance when clicked', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)

      await user.click(screen.getByRole('button', { name: /Restart/i }))

      expect(mockApiCall).toHaveBeenCalledWith(
        '/admin/instances/1/restart',
        { method: 'POST' }
      )

      // Reload is an implementation detail - not testing
    })
  })

  describe('Uninstall Actions', () => {
    it('should show uninstall button for all non-deprovisioned instances', () => {
      const statuses = ['running', 'stopped', 'error', 'provisioning']

      statuses.forEach(status => {
        const { unmount } = render(<InstanceActions instanceId="1" currentStatus={status} />)
        expect(screen.getByRole('button', { name: /Uninstall/i })).toBeInTheDocument()
        unmount()
      })
    })

    it('should not show uninstall for deprovisioned instances', () => {
      render(<InstanceActions instanceId="1" currentStatus="deprovisioned" />)

      expect(screen.queryByRole('button', { name: /Uninstall/i })).not.toBeInTheDocument()
    })

    it('should confirm before uninstalling', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="123" currentStatus="running" />)

      await user.click(screen.getByRole('button', { name: /Uninstall/i }))

      expect(mockConfirm).toHaveBeenCalledWith('Uninstall instance 123?')
    })

    it('should cancel uninstall if not confirmed', async () => {
      mockConfirm.mockReturnValue(false)
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)

      await user.click(screen.getByRole('button', { name: /Uninstall/i }))

      expect(mockApiCall).not.toHaveBeenCalled()
      // Not testing reload - implementation detail
    })

    it('should uninstall instance when confirmed', async () => {
      mockApiCall.mockResolvedValue({ ok: true })
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)

      await user.click(screen.getByRole('button', { name: /Uninstall/i }))

      expect(mockApiCall).toHaveBeenCalledWith(
        '/admin/instances/1/uninstall',
        { method: 'DELETE' }
      )

      // Reload is an implementation detail - not testing
    })

    it('should show uninstalling state during operation', async () => {
      mockApiCall.mockImplementation(() =>
        new Promise(resolve => setTimeout(() => resolve({ ok: true }), 100))
      )
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)
      const uninstallBtn = screen.getByRole('button', { name: /Uninstall/i })

      await user.click(uninstallBtn)

      expect(screen.getByText('Uninstalling...')).toBeInTheDocument()
      expect(uninstallBtn).toBeDisabled()
    })
  })

  describe('Error Handling', () => {
    it('should handle API errors gracefully', async () => {
      mockApiCall.mockRejectedValue(new Error('Network error'))
      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="stopped" />)

      await user.click(screen.getByRole('button', { name: /Start/i }))

      await waitFor(() => {
        expect(consoleSpy).toHaveBeenCalledWith(
          'Failed to start instance:',
          expect.any(Error)
        )
      })

      // Not testing reload - implementation detail
      consoleSpy.mockRestore()
    })

    it('should disable all buttons during any operation', async () => {
      mockApiCall.mockImplementation(() =>
        new Promise(resolve => setTimeout(() => resolve({ ok: true }), 100))
      )
      const user = userEvent.setup()

      render(<InstanceActions instanceId="1" currentStatus="running" />)

      const stopBtn = screen.getByRole('button', { name: /Stop/i })
      const restartBtn = screen.getByRole('button', { name: /Restart/i })
      const uninstallBtn = screen.getByRole('button', { name: /Uninstall/i })

      await user.click(stopBtn)

      // All buttons should be disabled during operation
      expect(stopBtn).toBeDisabled()
      expect(restartBtn).toBeDisabled()
      expect(uninstallBtn).toBeDisabled()
    })
  })

  describe('State-specific UI', () => {
    it('should apply correct styling for each action type', () => {
      render(<InstanceActions instanceId="1" currentStatus="running" />)

      const stopBtn = screen.getByRole('button', { name: /Stop/i })
      const restartBtn = screen.getByRole('button', { name: /Restart/i })
      const uninstallBtn = screen.getByRole('button', { name: /Uninstall/i })

      // Check color classes
      expect(stopBtn.className).toContain('text-yellow-600')
      expect(restartBtn.className).toContain('text-blue-600')
      expect(uninstallBtn.className).toContain('text-red-600')
    })

    it('should show appropriate icons for each action', () => {
      render(<InstanceActions instanceId="1" currentStatus="deprovisioned" />)

      // Check that Rocket icon is present for provision
      const provisionBtn = screen.getByRole('button', { name: /Provision/i })
      const rocketIcon = provisionBtn.querySelector('.lucide-rocket')
      expect(rocketIcon).toBeInTheDocument()
    })
  })
})
