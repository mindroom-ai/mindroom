import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import '@testing-library/jest-dom'
import { useRouter } from 'next/navigation'
import SettingsPage from '../page'
import * as api from '@/lib/api'
import { createClient } from '@/lib/supabase/client'

// Mock dependencies
jest.mock('next/navigation', () => ({
  useRouter: jest.fn()
}))

jest.mock('@/lib/api', () => ({
  getAccount: jest.fn(),
  exportUserData: jest.fn(),
  requestAccountDeletion: jest.fn(),
  cancelAccountDeletion: jest.fn(),
  updateConsent: jest.fn()
}))

jest.mock('@/lib/supabase/client', () => ({
  createClient: jest.fn()
}))

jest.mock('@/lib/logger', () => ({
  logger: {
    error: jest.fn(),
    log: jest.fn()
  }
}))

describe('SettingsPage - Edge Cases and Potential Bugs', () => {
  const mockRouter = {
    push: jest.fn()
  }

  const mockSupabase = {
    auth: {
      signOut: jest.fn()
    }
  }

  const mockAccount = {
    id: 'acc-123',
    email: 'test@example.com',
    status: 'active',
    deleted_at: null,
    consent_marketing: false,
    consent_analytics: false
  }

  beforeEach(() => {
    jest.clearAllMocks()
    jest.restoreAllMocks()
    jest.useFakeTimers()
    ;(useRouter as jest.Mock).mockReturnValue(mockRouter)
    ;(createClient as jest.Mock).mockReturnValue(mockSupabase)
    ;(api.getAccount as jest.Mock).mockResolvedValue(mockAccount)
  })

  afterEach(() => {
    jest.runOnlyPendingTimers()
    jest.useRealTimers()
  })

  describe('Race Conditions', () => {
    it('should debounce rapid consent checkbox clicks', async () => {
      ;(api.updateConsent as jest.Mock)
        .mockResolvedValueOnce({ status: 'success' })

      render(<SettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('Privacy Preferences')).toBeInTheDocument()
      })

      const marketingCheckbox = screen.getByRole('checkbox', { name: /marketing communications/i })

      // Rapidly click the checkbox multiple times
      fireEvent.click(marketingCheckbox)
      fireEvent.click(marketingCheckbox)
      fireEvent.click(marketingCheckbox)

      // Fast forward past debounce delay - wrap in act to handle state updates
      act(() => {
        jest.advanceTimersByTime(600)
      })

      await waitFor(() => {
        // With debouncing, should only make ONE API call despite 3 clicks
        expect(api.updateConsent).toHaveBeenCalledTimes(1)
        // And it should be called with the final state (3 clicks = on, off, on)
        expect(api.updateConsent).toHaveBeenCalledWith(true, false)
      })
    })

    it('should cleanup deletion timeout if component unmounts', async () => {
      const deletionResponse = {
        status: 'deletion_scheduled',
        grace_period_days: 7
      }
      ;(api.requestAccountDeletion as jest.Mock).mockResolvedValue(deletionResponse)

      const { unmount } = render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const confirmButton = screen.getByRole('button', { name: /yes, delete my account/i })
      fireEvent.click(confirmButton)

      await waitFor(() => {
        expect(api.requestAccountDeletion).toHaveBeenCalledWith(true)
      })

      // Unmount component before the 3-second timeout
      unmount()

      // Fast-forward time to see if the timeout still executes - wrap in act
      act(() => {
        jest.advanceTimersByTime(3500)
      })

      // After unmount and timeout, signOut should NOT have been called
      // because the cleanup should have cleared the timeout
      expect(mockSupabase.auth.signOut).not.toHaveBeenCalled()
      expect(mockRouter.push).not.toHaveBeenCalled()
    })

    it('should prevent multiple simultaneous deletion requests', async () => {
      ;(api.requestAccountDeletion as jest.Mock).mockImplementation(
        () => new Promise(resolve => setTimeout(() => resolve({
          status: 'deletion_scheduled',
          grace_period_days: 7
        }), 100))
      )

      render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const confirmButton = screen.getByRole('button', { name: /yes, delete my account/i })

      // Try to click confirm multiple times rapidly
      fireEvent.click(confirmButton)
      fireEvent.click(confirmButton)
      fireEvent.click(confirmButton)

      act(() => {
        jest.advanceTimersByTime(150)
      })

      await waitFor(() => {
        // Should only call the API once, but it might call multiple times
        // if the button isn't properly disabled
        expect(api.requestAccountDeletion).toHaveBeenCalledTimes(1)
      })
    })
  })

  describe('Error Message Details', () => {
    it('should show specific error messages to help users', async () => {
      const specificError = new Error('Network timeout - please check your connection')
      ;(api.exportUserData as jest.Mock).mockRejectedValue(specificError)

      render(<SettingsPage />)

      const exportButton = await screen.findByRole('button', { name: /export my data/i })
      fireEvent.click(exportButton)

      await waitFor(() => {
        // Now we show the specific error message to help users
        const errorMessage = screen.getByText(/failed to export data/i)
        expect(errorMessage).toBeInTheDocument()

        // The error message now includes the specific error details
        expect(errorMessage.textContent).toContain('Network timeout')
        expect(errorMessage.textContent).toContain('please check your connection')
      })
    })
  })

  describe('Memory Leaks', () => {
    it('should reuse single Supabase client instance', async () => {
      const deletionResponse = {
        status: 'deletion_scheduled',
        grace_period_days: 7
      }
      ;(api.requestAccountDeletion as jest.Mock).mockResolvedValue(deletionResponse)

      render(<SettingsPage />)

      // Wait for initial render and Supabase client creation
      await waitFor(() => {
        expect(screen.getByText('Settings')).toBeInTheDocument()
      })

      // Should create client once on mount
      expect(createClient).toHaveBeenCalledTimes(1)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const confirmButton = screen.getByRole('button', { name: /yes, delete my account/i })
      fireEvent.click(confirmButton)

      await waitFor(() => {
        expect(api.requestAccountDeletion).toHaveBeenCalled()
      })

      act(() => {
        jest.advanceTimersByTime(3500)
      })

      // Should still only have created one client (reused the existing one)
      expect(createClient).toHaveBeenCalledTimes(1)
    })
  })
})
