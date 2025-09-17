import { render, screen, fireEvent, waitFor } from '@testing-library/react'
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

// Mock URL.createObjectURL and URL.revokeObjectURL
global.URL.createObjectURL = jest.fn(() => 'blob:mock-url')
global.URL.revokeObjectURL = jest.fn()

describe('SettingsPage', () => {
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
    consent_analytics: true
  }

  beforeEach(() => {
    jest.clearAllMocks()
    jest.restoreAllMocks()
    ;(useRouter as jest.Mock).mockReturnValue(mockRouter)
    ;(createClient as jest.Mock).mockReturnValue(mockSupabase)
    ;(api.getAccount as jest.Mock).mockResolvedValue(mockAccount)
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  describe('Initial Rendering', () => {
    it('should render the settings page with all sections', async () => {
      render(<SettingsPage />)

      // Check main title
      expect(screen.getByText('Settings')).toBeInTheDocument()

      // Wait for account info to load
      await waitFor(() => {
        expect(api.getAccount).toHaveBeenCalled()
      })

      // Check sections are present
      expect(screen.getByText('Privacy & Data')).toBeInTheDocument()
      expect(screen.getByText('Data Export')).toBeInTheDocument()
      expect(screen.getByText('Privacy Preferences')).toBeInTheDocument()
      expect(screen.getByText('Data Retention')).toBeInTheDocument()
      expect(screen.getByText('Danger Zone')).toBeInTheDocument()
    })

    it('should load and display account consent preferences', async () => {
      render(<SettingsPage />)

      await waitFor(() => {
        const marketingCheckbox = screen.getByRole('checkbox', { name: /marketing communications/i })
        const analyticsCheckbox = screen.getByRole('checkbox', { name: /usage analytics/i })

        expect(marketingCheckbox).not.toBeChecked()
        expect(analyticsCheckbox).toBeChecked()
      })
    })

    it('should show deletion warning if account is pending deletion', async () => {
      const deletedAccount = { ...mockAccount, status: 'pending_deletion' }
      ;(api.getAccount as jest.Mock).mockResolvedValue(deletedAccount)

      render(<SettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('Account Deletion Pending')).toBeInTheDocument()
        expect(screen.getByText(/scheduled for deletion/i)).toBeInTheDocument()
        expect(screen.getByText('Cancel Deletion Request')).toBeInTheDocument()
      })

      // Danger Zone should not be shown
      expect(screen.queryByText('Danger Zone')).not.toBeInTheDocument()
    })
  })

  describe('Data Export', () => {
    it('should export user data successfully', async () => {
      const exportData = {
        export_date: '2025-01-15T00:00:00Z',
        account_id: 'acc-123',
        personal_data: { email: 'test@example.com' }
      }
      ;(api.exportUserData as jest.Mock).mockResolvedValue(exportData)

      render(<SettingsPage />)

      const exportButton = await screen.findByRole('button', { name: /export my data/i })
      fireEvent.click(exportButton)

      await waitFor(() => {
        expect(api.exportUserData).toHaveBeenCalled()
        expect(screen.getByText(/data has been exported successfully/i)).toBeInTheDocument()
      })

      // The download functionality is tested implicitly by the success message
      // Testing actual file download would require more complex browser API mocking
    })

    it('should handle export failure', async () => {
      ;(api.exportUserData as jest.Mock).mockRejectedValue(new Error('Export failed'))

      render(<SettingsPage />)

      const exportButton = await screen.findByRole('button', { name: /export my data/i })
      fireEvent.click(exportButton)

      await waitFor(() => {
        expect(screen.getByText(/failed to export data/i)).toBeInTheDocument()
      })
    })

    it('should show loading state during export', async () => {
      ;(api.exportUserData as jest.Mock).mockImplementation(
        () => new Promise(resolve => setTimeout(resolve, 100))
      )

      render(<SettingsPage />)

      const exportButton = await screen.findByRole('button', { name: /export my data/i })
      fireEvent.click(exportButton)

      expect(screen.getByText('Exporting...')).toBeInTheDocument()
      expect(exportButton).toBeDisabled()
    })
  })

  describe('Account Deletion', () => {
    it('should show confirmation dialog on first click', async () => {
      render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      expect(screen.getByText('Are you absolutely sure?')).toBeInTheDocument()
      expect(screen.getByText(/schedule your account for deletion/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /yes, delete my account/i })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument()
    })

    it('should cancel deletion confirmation', async () => {
      render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const cancelButton = screen.getByRole('button', { name: /cancel/i })
      fireEvent.click(cancelButton)

      expect(screen.queryByText('Are you absolutely sure?')).not.toBeInTheDocument()
    })

    it('should request account deletion with confirmation', async () => {
      const deletionResponse = {
        status: 'deletion_scheduled',
        grace_period_days: 7
      }
      ;(api.requestAccountDeletion as jest.Mock).mockResolvedValue(deletionResponse)

      render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const confirmButton = screen.getByRole('button', { name: /yes, delete my account/i })
      fireEvent.click(confirmButton)

      await waitFor(() => {
        expect(api.requestAccountDeletion).toHaveBeenCalledWith(true)
        expect(screen.getByText(/account deletion scheduled/i)).toBeInTheDocument()
        expect(screen.getByText(/7 days to cancel/i)).toBeInTheDocument()
      })

      // Should sign out and redirect after 3 seconds
      await waitFor(() => {
        expect(mockSupabase.auth.signOut).toHaveBeenCalled()
        expect(mockRouter.push).toHaveBeenCalledWith('/login')
      }, { timeout: 4000 })
    })

    it('should handle deletion request failure', async () => {
      ;(api.requestAccountDeletion as jest.Mock).mockRejectedValue(new Error('Failed'))

      render(<SettingsPage />)

      const deleteButton = await screen.findByRole('button', { name: /delete account/i })
      fireEvent.click(deleteButton)

      const confirmButton = screen.getByRole('button', { name: /yes, delete my account/i })
      fireEvent.click(confirmButton)

      await waitFor(() => {
        expect(screen.getByText(/failed to request account deletion/i)).toBeInTheDocument()
      })
    })
  })

  describe('Cancel Deletion', () => {
    it('should cancel account deletion successfully', async () => {
      const deletedAccount = { ...mockAccount, deleted_at: '2025-01-01T00:00:00Z' }
      ;(api.getAccount as jest.Mock).mockResolvedValue(deletedAccount)
      ;(api.cancelAccountDeletion as jest.Mock).mockResolvedValue({ status: 'success' })

      render(<SettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('Account Deletion Pending')).toBeInTheDocument()
      })

      const cancelButton = screen.getByRole('button', { name: /cancel deletion request/i })
      fireEvent.click(cancelButton)

      await waitFor(() => {
        expect(api.cancelAccountDeletion).toHaveBeenCalled()
        expect(screen.getByText(/deletion has been cancelled/i)).toBeInTheDocument()
      })
    })

    it('should handle cancellation failure', async () => {
      const deletedAccount = { ...mockAccount, deleted_at: '2025-01-01T00:00:00Z' }
      ;(api.getAccount as jest.Mock).mockResolvedValue(deletedAccount)
      ;(api.cancelAccountDeletion as jest.Mock).mockRejectedValue(new Error('Failed'))

      render(<SettingsPage />)

      await waitFor(() => {
        expect(screen.getByText('Account Deletion Pending')).toBeInTheDocument()
      })

      const cancelButton = screen.getByRole('button', { name: /cancel deletion request/i })
      fireEvent.click(cancelButton)

      await waitFor(() => {
        expect(screen.getByText(/failed to cancel deletion/i)).toBeInTheDocument()
      })
    })
  })

  describe('Consent Management', () => {
    it('should update marketing consent', async () => {
      ;(api.updateConsent as jest.Mock).mockResolvedValue({ status: 'success' })

      render(<SettingsPage />)

      // Wait for account data to load and checkboxes to be set correctly
      await waitFor(() => {
        const analyticsCheckbox = screen.getByRole('checkbox', { name: /usage analytics/i })
        expect(analyticsCheckbox).toBeChecked()
      })

      const marketingCheckbox = screen.getByRole('checkbox', { name: /marketing communications/i })
      expect(marketingCheckbox).not.toBeChecked()

      fireEvent.click(marketingCheckbox)

      await waitFor(() => {
        expect(api.updateConsent).toHaveBeenCalledWith(true, true) // marketing true, analytics stays true per mock
        expect(screen.getByText(/privacy preferences updated/i)).toBeInTheDocument()
      })
    })

    it('should update analytics consent', async () => {
      ;(api.updateConsent as jest.Mock).mockResolvedValue({ status: 'success' })

      render(<SettingsPage />)

      // Wait for account data to load and checkboxes to be set correctly
      await waitFor(() => {
        const analyticsCheckbox = screen.getByRole('checkbox', { name: /usage analytics/i })
        expect(analyticsCheckbox).toBeChecked()
      })

      const analyticsCheckbox = screen.getByRole('checkbox', { name: /usage analytics/i })
      fireEvent.click(analyticsCheckbox)

      await waitFor(() => {
        expect(api.updateConsent).toHaveBeenCalledWith(false, false) // marketing false, analytics toggled to false
        expect(screen.getByText(/privacy preferences updated/i)).toBeInTheDocument()
      })
    })

    it('should revert consent on update failure', async () => {
      ;(api.updateConsent as jest.Mock).mockRejectedValue(new Error('Failed'))

      render(<SettingsPage />)

      await waitFor(() => {
        expect(api.getAccount).toHaveBeenCalled()
      })

      const marketingCheckbox = screen.getByRole('checkbox', { name: /marketing communications/i })
      expect(marketingCheckbox).not.toBeChecked()

      fireEvent.click(marketingCheckbox)

      await waitFor(() => {
        expect(screen.getByText(/failed to update preferences/i)).toBeInTheDocument()
        // Checkbox should revert to unchecked
        expect(marketingCheckbox).not.toBeChecked()
      })
    })
  })

  describe('Data Retention Display', () => {
    it('should display data retention policy', async () => {
      render(<SettingsPage />)

      // Wait for the page to load (check for a main element first)
      await waitFor(() => {
        expect(screen.getByText('Data Retention')).toBeInTheDocument()
      })

      // Now check for the specific retention policy texts
      expect(screen.getByText(/Personal data:/, { exact: false })).toBeInTheDocument()
      expect(screen.getByText(/Deleted immediately when you close your account/)).toBeInTheDocument()
      expect(screen.getByText(/Payment info:/, { exact: false })).toBeInTheDocument()
      expect(screen.getByText(/We don't store payment details - Stripe handles this/)).toBeInTheDocument()
      expect(screen.getByText(/Invoices:/, { exact: false })).toBeInTheDocument()
      expect(screen.getByText(/Only invoice numbers kept \(anonymized\) for tax compliance/)).toBeInTheDocument()
    })
  })

  describe('Loading States', () => {
    it('should disable buttons during operations', async () => {
      ;(api.exportUserData as jest.Mock).mockImplementation(
        () => new Promise(resolve => setTimeout(resolve, 100))
      )

      render(<SettingsPage />)

      const exportButton = await screen.findByRole('button', { name: /export my data/i })
      fireEvent.click(exportButton)

      // Button should be disabled during loading
      expect(exportButton).toBeDisabled()
      expect(screen.getByText('Exporting...')).toBeInTheDocument()
    })
  })

  describe('Error Handling', () => {
    it('should handle account loading failure gracefully', async () => {
      ;(api.getAccount as jest.Mock).mockRejectedValue(new Error('Failed to load'))

      render(<SettingsPage />)

      // Page should still render without account info
      await waitFor(() => {
        expect(screen.getByText('Settings')).toBeInTheDocument()
        expect(screen.getByText('Privacy & Data')).toBeInTheDocument()
      })
    })
  })
})
