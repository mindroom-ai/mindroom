import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InstanceCard } from '../InstanceCard'
import { provisionInstance } from '@/lib/api'

// Define the Instance type inline to avoid import issues
type Instance = {
  instance_id: number
  account_id: string
  status: 'running' | 'provisioning' | 'stopped' | 'error' | 'failed'
  frontend_url: string | null
  backend_url: string | null
  matrix_server_url: string | null
  tier: string | null
  created_at: string
  updated_at: string
}

// Mock the API module
jest.mock('@/lib/api', () => ({
  provisionInstance: jest.fn()
}))

// Mock window functions
const mockReload = jest.fn()
const mockAlert = jest.fn()
const mockClipboardWriteText = jest.fn()

// Mock location.reload
const originalLocation = window.location
beforeAll(() => {
  delete (window as any).location
  window.location = { ...originalLocation, reload: mockReload }
})
afterAll(() => {
  window.location = originalLocation
})

window.alert = mockAlert

Object.defineProperty(navigator, 'clipboard', {
  value: { writeText: mockClipboardWriteText },
  writable: true,
  configurable: true
})

describe('InstanceCard', () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockClipboardWriteText.mockResolvedValue(undefined)
  })

  describe('No Instance State', () => {
    it('should show provision prompt when no instance exists', () => {
      render(<InstanceCard instance={null} />)

      expect(screen.getByText('No instance provisioned yet')).toBeInTheDocument()
      expect(screen.getByText(/Click below to create your MindRoom instance/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Provision Instance/i })).toBeInTheDocument()
    })

    it('should handle successful provisioning', async () => {
      const mockProvisionResult = { instance_id: 1, status: 'provisioning' }
      ;(provisionInstance as jest.Mock).mockResolvedValueOnce(mockProvisionResult)

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await userEvent.click(button)

      // Check loading state
      expect(screen.getByText('Provisioning...')).toBeInTheDocument()

      await waitFor(() => {
        expect(provisionInstance).toHaveBeenCalled()
        expect(mockReload).toHaveBeenCalled()
      })
    })

    it('should handle provisioning errors', async () => {
      const error = new Error('No subscription found')
      ;(provisionInstance as jest.Mock).mockRejectedValueOnce(error)

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await userEvent.click(button)

      await waitFor(() => {
        expect(mockAlert).toHaveBeenCalledWith(
          'Please wait for your account setup to complete, then try again.'
        )
      })
    })

    it('should handle generic provisioning errors', async () => {
      const error = new Error('Server error')
      ;(provisionInstance as jest.Mock).mockRejectedValueOnce(error)

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await userEvent.click(button)

      await waitFor(() => {
        expect(mockAlert).toHaveBeenCalledWith('Failed to provision instance: Server error')
      })
    })

    it('should ignore aborted requests', async () => {
      const abortError = new Error('aborted')
      abortError.name = 'AbortError'
      ;(provisionInstance as jest.Mock).mockRejectedValueOnce(abortError)

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await userEvent.click(button)

      await waitFor(() => {
        expect(provisionInstance).toHaveBeenCalled()
      })

      // Should not show alert for aborted requests
      expect(mockAlert).not.toHaveBeenCalled()
    })
  })

  describe('Instance Display', () => {
    const mockInstance: Instance = {
      instance_id: 1,
      account_id: 'acc-123',
      status: 'running',
      frontend_url: 'https://customer.mindroom.chat',
      backend_url: 'https://api.customer.mindroom.chat',
      matrix_server_url: 'https://matrix.customer.mindroom.chat',
      tier: 'pro',
      created_at: new Date().toISOString(),
      updated_at: new Date(Date.now() - 3600000).toISOString() // 1 hour ago
    }

    it('should display instance information correctly', () => {
      render(<InstanceCard instance={mockInstance} />)

      expect(screen.getByText('MindRoom Instance')).toBeInTheDocument()
      expect(screen.getByText('Running')).toBeInTheDocument()
      expect(screen.getByText('customer.mindroom.chat')).toBeInTheDocument()
      expect(screen.getByText('api.customer.mindroom.chat')).toBeInTheDocument()
      expect(screen.getByText('matrix.customer.mindroom.chat')).toBeInTheDocument()
      expect(screen.getByText('Pro')).toBeInTheDocument()
      expect(screen.getByText('#1')).toBeInTheDocument()
    })

    it('should show correct status indicators', () => {
      const statuses: Array<[Instance['status'], string]> = [
        ['running', 'Running'],
        ['provisioning', 'Provisioning...'],
        ['stopped', 'Stopped'],
        ['error', 'Error'],
        ['failed', 'Error']
      ]

      statuses.forEach(([status, expectedText]) => {
        const { rerender } = render(
          <InstanceCard instance={{ ...mockInstance, status }} />
        )
        expect(screen.getByText(expectedText)).toBeInTheDocument()
        rerender(<InstanceCard instance={null} />)
      })
    })

    it('should format relative time correctly', () => {
      const testCases = [
        { offset: 30000, expected: 'just now' }, // 30 seconds
        { offset: 300000, expected: '5m ago' }, // 5 minutes
        { offset: 7200000, expected: '2h ago' }, // 2 hours
        { offset: 172800000, expected: '2d ago' } // 2 days
      ]

      testCases.forEach(({ offset, expected }) => {
        const updatedAt = new Date(Date.now() - offset).toISOString()
        const { rerender } = render(
          <InstanceCard instance={{ ...mockInstance, updated_at: updatedAt }} />
        )
        expect(screen.getByText(new RegExp(expected))).toBeInTheDocument()
        rerender(<InstanceCard instance={null} />)
      })
    })

    it('should handle URL parsing errors gracefully', () => {
      const instanceWithBadUrl = {
        ...mockInstance,
        frontend_url: 'not-a-valid-url'
      }

      render(<InstanceCard instance={instanceWithBadUrl} />)

      // Should still render without crashing
      expect(screen.getByText('MindRoom Instance')).toBeInTheDocument()
      expect(screen.getByText('Open')).toBeInTheDocument()
    })

    it('should show Open MindRoom button for running instances', () => {
      render(<InstanceCard instance={mockInstance} />)

      const openButton = screen.getByRole('link', { name: /Open MindRoom/i })
      expect(openButton).toBeInTheDocument()
      expect(openButton).toHaveAttribute('href', mockInstance.frontend_url)
      expect(openButton).toHaveAttribute('target', '_blank')
    })

    it('should not show Open MindRoom button for non-running instances', () => {
      const stoppedInstance = { ...mockInstance, status: 'stopped' as Instance['status'] }
      render(<InstanceCard instance={stoppedInstance} />)

      expect(screen.queryByRole('link', { name: /Open MindRoom/i })).not.toBeInTheDocument()
    })
  })

  describe('Copy to Clipboard', () => {
    const mockInstance: Instance = {
      instance_id: 1,
      account_id: 'acc-123',
      status: 'running',
      frontend_url: 'https://customer.mindroom.chat',
      backend_url: 'https://api.customer.mindroom.chat',
      matrix_server_url: 'https://matrix.customer.mindroom.chat',
      tier: 'pro',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    }

    it('should copy domain to clipboard', async () => {
      render(<InstanceCard instance={mockInstance} />)

      const copyButtons = screen.getAllByTitle('Copy domain')
      await userEvent.click(copyButtons[0])

      expect(mockClipboardWriteText).toHaveBeenCalledWith('customer.mindroom.chat')
    })

    it('should copy frontend URL to clipboard', async () => {
      render(<InstanceCard instance={mockInstance} />)

      const copyButton = screen.getByTitle('Copy URL')
      await userEvent.click(copyButton)

      expect(mockClipboardWriteText).toHaveBeenCalledWith('https://customer.mindroom.chat')
    })

    it('should copy API URL to clipboard', async () => {
      render(<InstanceCard instance={mockInstance} />)

      const copyButton = screen.getByTitle('Copy API URL')
      await userEvent.click(copyButton)

      expect(mockClipboardWriteText).toHaveBeenCalledWith('https://api.customer.mindroom.chat')
    })

    it('should copy Matrix URL to clipboard', async () => {
      render(<InstanceCard instance={mockInstance} />)

      const copyButton = screen.getByTitle('Copy Matrix URL')
      await userEvent.click(copyButton)

      expect(mockClipboardWriteText).toHaveBeenCalledWith('https://matrix.customer.mindroom.chat')
    })

    it('should handle clipboard API errors gracefully', async () => {
      mockClipboardWriteText.mockRejectedValueOnce(new Error('Clipboard access denied'))

      render(<InstanceCard instance={mockInstance} />)

      const copyButton = screen.getByTitle('Copy domain')
      await userEvent.click(copyButton)

      // Should not throw or show error
      expect(mockAlert).not.toHaveBeenCalled()
    })
  })

  describe('Edge Cases', () => {
    it('should handle missing optional fields', () => {
      const minimalInstance: Instance = {
        instance_id: 1,
        account_id: 'acc-123',
        status: 'running',
        frontend_url: null,
        backend_url: null,
        matrix_server_url: null,
        tier: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      }

      render(<InstanceCard instance={minimalInstance} />)

      expect(screen.getByText('MindRoom Instance')).toBeInTheDocument()
      expect(screen.getByText('Free')).toBeInTheDocument() // Default tier
      expect(screen.queryByText('Domain')).not.toBeInTheDocument()
      expect(screen.queryByText('Frontend')).not.toBeInTheDocument()
      expect(screen.queryByText('API')).not.toBeInTheDocument()
      expect(screen.queryByText('Matrix Server')).not.toBeInTheDocument()
    })

    it('should handle unknown status gracefully', () => {
      const unknownStatusInstance = {
        ...mockInstance,
        status: 'unknown-status' as any
      }

      render(<InstanceCard instance={unknownStatusInstance} />)

      expect(screen.getByText('unknown-status')).toBeInTheDocument()
    })

    it('should disable provision button while provisioning', async () => {
      ;(provisionInstance as jest.Mock).mockImplementation(
        () => new Promise(resolve => setTimeout(resolve, 1000))
      )

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await userEvent.click(button)

      expect(button).toBeDisabled()
      expect(screen.getByText('Provisioning...')).toBeInTheDocument()
    })
  })

  const mockInstance: Instance = {
    instance_id: 1,
    account_id: 'acc-123',
    status: 'running',
    frontend_url: 'https://customer.mindroom.chat',
    backend_url: 'https://api.customer.mindroom.chat',
    matrix_server_url: 'https://matrix.customer.mindroom.chat',
    tier: 'pro',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  }
})
