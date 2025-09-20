/**
 * @jest-environment jsdom
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { InstanceCard } from '../InstanceCard'
import { provisionInstance } from '@/lib/api'

// Define the Instance type
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
  kubernetes_synced_at?: string | null
  status_hint?: string | null
}

// Mock the API
jest.mock('@/lib/api', () => ({
  provisionInstance: jest.fn()
}))

describe('InstanceCard - Simplified Tests', () => {
  const mockProvisionInstance = provisionInstance as jest.Mock

  beforeEach(() => {
    jest.clearAllMocks()
    // Mock console to avoid noise
    jest.spyOn(console, 'log').mockImplementation()
    jest.spyOn(console, 'error').mockImplementation()
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  describe('When no instance exists', () => {
    it('should display provision button and message', () => {
      render(<InstanceCard instance={null} />)

      expect(screen.getByText(/No instance provisioned yet/)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Provision Instance/i })).toBeInTheDocument()
    })

    it('should call provisionInstance when button is clicked', async () => {
      mockProvisionInstance.mockResolvedValue({ instance_id: 1 })
      const user = userEvent.setup()

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await user.click(button)

      await waitFor(() => {
        expect(mockProvisionInstance).toHaveBeenCalled()
      })
    })

    it('should show loading state while provisioning', async () => {
      mockProvisionInstance.mockImplementation(() =>
        new Promise(resolve => setTimeout(resolve, 100))
      )
      const user = userEvent.setup()

      render(<InstanceCard instance={null} />)
      const button = screen.getByRole('button', { name: /Provision Instance/i })

      await user.click(button)

      expect(screen.getByText('Provisioning...')).toBeInTheDocument()
      expect(button).toBeDisabled()
    })
  })

  describe('When instance exists', () => {
    const mockInstance: Instance = {
      instance_id: 1,
      account_id: 'acc-123',
      status: 'running',
      frontend_url: 'https://customer.mindroom.chat',
      backend_url: 'https://customer.api.mindroom.chat',
      matrix_server_url: 'https://customer.matrix.mindroom.chat',
      tier: 'pro',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString()
    }

    it('should display instance information', () => {
      render(<InstanceCard instance={mockInstance} />)

      // Check header
      expect(screen.getByText('MindRoom Instance')).toBeInTheDocument()

      // Check status
      expect(screen.getByText('Running')).toBeInTheDocument()

      // Check URLs are displayed (use getAllByText since domain appears multiple times)
      expect(screen.getAllByText('customer.mindroom.chat')).toHaveLength(2) // Domain and Frontend
      expect(screen.getByText('customer.api.mindroom.chat')).toBeInTheDocument()
      expect(screen.getByText('customer.matrix.mindroom.chat')).toBeInTheDocument()

      // Check tier (it's showing as 'pro' not 'Pro' due to capitalize class)
      expect(screen.getByText(/pro/i)).toBeInTheDocument()

      // Check instance ID
      expect(screen.getByText('#1')).toBeInTheDocument()
    })

    it('should show different status messages', () => {
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

    it('should display Open MindRoom button for running instances', () => {
      render(<InstanceCard instance={mockInstance} />)

      const openButton = screen.getByRole('link', { name: /Open MindRoom/i })
      expect(openButton).toBeInTheDocument()
      expect(openButton).toHaveAttribute('href', 'https://customer.mindroom.chat')
    })

    it('should not display Open MindRoom button for stopped instances', () => {
      const stoppedInstance = { ...mockInstance, status: 'stopped' as Instance['status'] }
      render(<InstanceCard instance={stoppedInstance} />)

      expect(screen.queryByRole('link', { name: /Open MindRoom/i })).not.toBeInTheDocument()
    })

    it('should handle missing URLs gracefully', () => {
      const minimalInstance: Instance = {
        ...mockInstance,
        frontend_url: null,
        backend_url: null,
        matrix_server_url: null
      }

      render(<InstanceCard instance={minimalInstance} />)

      // Should still render without crashing
      expect(screen.getByText('MindRoom Instance')).toBeInTheDocument()
      expect(screen.getByText('Running')).toBeInTheDocument()

      // Should not show URL sections
      expect(screen.queryByText('Domain')).not.toBeInTheDocument()
      expect(screen.queryByText('Frontend')).not.toBeInTheDocument()
      expect(screen.queryByText('API')).not.toBeInTheDocument()
    })

    it('should show default tier when not specified', () => {
      const noTierInstance = { ...mockInstance, tier: null }
      render(<InstanceCard instance={noTierInstance} />)

      expect(screen.getByText('Free')).toBeInTheDocument()
    })
  })

  describe('Relative time formatting', () => {
    const mockInstance: Instance = {
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

    it('should show "just now" for recent updates', () => {
      render(<InstanceCard instance={mockInstance} />)
      expect(screen.getByText(/just now/)).toBeInTheDocument()
    })

    it('should show minutes ago', () => {
      const fiveMinutesAgo = new Date(Date.now() - 5 * 60 * 1000).toISOString()
      const instance = { ...mockInstance, updated_at: fiveMinutesAgo }

      render(<InstanceCard instance={instance} />)
      expect(screen.getByText(/5m ago/)).toBeInTheDocument()
    })

    it('should show hours ago', () => {
      const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString()
      const instance = { ...mockInstance, updated_at: twoHoursAgo }

      render(<InstanceCard instance={instance} />)
      expect(screen.getByText(/2h ago/)).toBeInTheDocument()
    })

    it('should show days ago', () => {
      const threeDaysAgo = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString()
      const instance = { ...mockInstance, updated_at: threeDaysAgo }

      render(<InstanceCard instance={instance} />)
      expect(screen.getByText(/3d ago/)).toBeInTheDocument()
    })
  })
})
