/**
 * Tests for Admin Accounts Page
 * This page handles critical admin operations:
 * - View all user accounts
 * - Update account status
 * - Delete accounts completely (with all resources)
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AccountsPage from '../page'
import { apiCall } from '@/lib/api'

// Mock the API module
jest.mock('@/lib/api', () => ({
  apiCall: jest.fn()
}))

// Mock window functions
const mockConfirm = jest.fn()
const mockAlert = jest.fn()
window.confirm = mockConfirm
window.alert = mockAlert

describe('Admin Accounts Page', () => {
  const mockApiCall = apiCall as jest.Mock

  const mockAccounts = {
    data: [
      {
        id: 'account_1',
        email: 'user1@example.com',
        full_name: 'User One',
        company_name: 'Company A',
        status: 'active',
        is_admin: false,
        created_at: '2024-01-01T00:00:00Z'
      },
      {
        id: 'account_2',
        email: 'admin@example.com',
        full_name: 'Admin User',
        company_name: null,
        status: 'active',
        is_admin: true,
        created_at: '2024-01-02T00:00:00Z'
      }
    ],
    total: 2
  }

  beforeEach(() => {
    jest.clearAllMocks()
    mockConfirm.mockReturnValue(false) // Default to not confirming
  })

  describe('Account Display', () => {
    it('should fetch and display accounts on mount', async () => {
      mockApiCall.mockResolvedValue({
        ok: true,
        json: async () => mockAccounts
      })

      render(<AccountsPage />)

      // Should show loading initially
      expect(screen.getByText('Loading...')).toBeInTheDocument()

      // Wait for accounts to load
      await waitFor(() => {
        expect(screen.getByText('user1@example.com')).toBeInTheDocument()
      })

      expect(screen.getByText('admin@example.com')).toBeInTheDocument()
      expect(screen.getByText('User One')).toBeInTheDocument()
      expect(screen.getByText('Admin User')).toBeInTheDocument()
      expect(screen.getByText('Company A')).toBeInTheDocument()

      // Check admin indicator
      const adminRows = screen.getAllByText('✓')
      expect(adminRows).toHaveLength(1) // Only one admin
    })

    it('should handle empty accounts list', async () => {
      mockApiCall.mockResolvedValue({
        ok: true,
        json: async () => ({ data: [], total: 0 })
      })

      render(<AccountsPage />)

      await waitFor(() => {
        expect(screen.getByText('No accounts found')).toBeInTheDocument()
      })
    })

    it('should handle fetch error gracefully', async () => {
      mockApiCall.mockRejectedValue(new Error('Network error'))
      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()

      render(<AccountsPage />)

      await waitFor(() => {
        expect(screen.queryByText('Loading...')).not.toBeInTheDocument()
      })

      expect(consoleSpy).toHaveBeenCalledWith(
        'Error fetching accounts:',
        expect.any(Error)
      )

      consoleSpy.mockRestore()
    })
  })

  describe('Account Deletion', () => {
    beforeEach(async () => {
      mockApiCall.mockResolvedValue({
        ok: true,
        json: async () => mockAccounts
      })

      render(<AccountsPage />)

      await waitFor(() => {
        expect(screen.getByText('user1@example.com')).toBeInTheDocument()
      })
    })

    it('should show delete button for each account', async () => {
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })
      expect(deleteButtons).toHaveLength(2) // One for each account
    })

    it('should show detailed confirmation dialog when delete is clicked', async () => {
      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      expect(mockConfirm).toHaveBeenCalledWith(
        expect.stringContaining('user1@example.com')
      )
      expect(mockConfirm).toHaveBeenCalledWith(
        expect.stringContaining('Deprovision all MindRoom instances')
      )
      expect(mockConfirm).toHaveBeenCalledWith(
        expect.stringContaining('Cancel any active Stripe subscriptions')
      )
      expect(mockConfirm).toHaveBeenCalledWith(
        expect.stringContaining('This action cannot be undone')
      )
    })

    it('should not delete account if user cancels confirmation', async () => {
      mockConfirm.mockReturnValue(false)
      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      expect(mockApiCall).not.toHaveBeenCalledWith(
        expect.stringContaining('/complete'),
        expect.any(Object)
      )
    })

    it('should delete account when confirmed', async () => {
      mockConfirm.mockReturnValue(true)
      mockApiCall.mockImplementation((url) => {
        if (url === '/admin/accounts') {
          return Promise.resolve({
            ok: true,
            json: async () => mockAccounts
          })
        }
        if (url === '/admin/accounts/account_1/complete') {
          return Promise.resolve({ ok: true })
        }
        return Promise.reject(new Error('Unexpected URL'))
      })

      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      await waitFor(() => {
        expect(mockApiCall).toHaveBeenCalledWith(
          '/admin/accounts/account_1/complete',
          { method: 'DELETE' }
        )
      })

      expect(mockAlert).toHaveBeenCalledWith(
        'Successfully deleted account user1@example.com and all associated resources.'
      )

      // Account should be removed from the list
      await waitFor(() => {
        expect(screen.queryByText('user1@example.com')).not.toBeInTheDocument()
      })
    })

    it('should show deleting state while deletion is in progress', async () => {
      mockConfirm.mockReturnValue(true)
      let resolveDelete: any
      mockApiCall.mockImplementation((url) => {
        if (url === '/admin/accounts') {
          return Promise.resolve({
            ok: true,
            json: async () => mockAccounts
          })
        }
        if (url === '/admin/accounts/account_1/complete') {
          return new Promise((resolve) => {
            resolveDelete = resolve
          })
        }
        return Promise.reject(new Error('Unexpected URL'))
      })

      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      await waitFor(() => {
        expect(screen.getByText('Deleting...')).toBeInTheDocument()
      })

      // Button should be disabled during deletion
      const deletingButton = screen.getByText('Deleting...').closest('button')
      expect(deletingButton).toBeDisabled()

      // Complete the deletion
      resolveDelete({ ok: true })

      await waitFor(() => {
        expect(screen.queryByText('Deleting...')).not.toBeInTheDocument()
      })
    })

    it('should handle deletion error gracefully', async () => {
      mockConfirm.mockReturnValue(true)
      const errorMessage = 'Failed to delete account'
      mockApiCall.mockImplementation((url) => {
        if (url === '/admin/accounts') {
          return Promise.resolve({
            ok: true,
            json: async () => mockAccounts
          })
        }
        if (url === '/admin/accounts/account_1/complete') {
          return Promise.resolve({
            ok: false,
            text: async () => errorMessage
          })
        }
        return Promise.reject(new Error('Unexpected URL'))
      })

      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()
      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      await waitFor(() => {
        expect(mockAlert).toHaveBeenCalledWith(
          `Failed to delete account: ${errorMessage}`
        )
      })

      expect(consoleSpy).toHaveBeenCalledWith(
        'Failed to delete account:',
        errorMessage
      )

      // Account should still be in the list
      expect(screen.getByText('user1@example.com')).toBeInTheDocument()

      consoleSpy.mockRestore()
    })

    it('should handle network error during deletion', async () => {
      mockConfirm.mockReturnValue(true)
      mockApiCall.mockImplementation((url) => {
        if (url === '/admin/accounts') {
          return Promise.resolve({
            ok: true,
            json: async () => mockAccounts
          })
        }
        if (url === '/admin/accounts/account_1/complete') {
          return Promise.reject(new Error('Network error'))
        }
        return Promise.reject(new Error('Unexpected URL'))
      })

      const consoleSpy = jest.spyOn(console, 'error').mockImplementation()
      const user = userEvent.setup()
      const deleteButtons = screen.getAllByRole('button', { name: /Delete/i })

      await user.click(deleteButtons[0])

      await waitFor(() => {
        expect(mockAlert).toHaveBeenCalledWith(
          'An error occurred while deleting the account'
        )
      })

      expect(consoleSpy).toHaveBeenCalledWith(
        'Error deleting account:',
        expect.any(Error)
      )

      consoleSpy.mockRestore()
    })
  })

  describe('Status Update', () => {
    beforeEach(async () => {
      mockApiCall.mockResolvedValue({
        ok: true,
        json: async () => mockAccounts
      })

      render(<AccountsPage />)

      await waitFor(() => {
        expect(screen.getByText('user1@example.com')).toBeInTheDocument()
      })
    })

    it('should allow status change via dropdown', async () => {
      const user = userEvent.setup()
      const statusSelects = screen.getAllByRole('combobox')

      // Change first account's status
      await user.selectOptions(statusSelects[0], 'suspended')

      // Find and click the save button for the first account
      const saveButtons = screen.getAllByText('Save')
      await user.click(saveButtons[0])

      await waitFor(() => {
        expect(mockApiCall).toHaveBeenCalledWith(
          '/admin/accounts/account_1/status?status=suspended',
          { method: 'PUT' }
        )
      })
    })
  })
})
