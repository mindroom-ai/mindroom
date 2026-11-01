import { render, screen, within } from '@testing-library/react'
import AdminDashboard from '../page'
import { apiCall } from '@/lib/api'
import { logger } from '@/lib/logger'

jest.mock('@/lib/api', () => ({
  apiCall: jest.fn(),
}))

jest.mock('@/lib/logger', () => ({
  logger: { error: jest.fn() },
}))

describe('AdminDashboard', () => {
  const mockApiCall = apiCall as jest.Mock

  const METRICS = {
    total_accounts: 1500,
    active_subscriptions: 1200,
    instances_by_status: { running: 1100, stopped: 2 },
    subscription_revenue: 199,
    recent_instances: [],
  }

  function mockResponses(metrics?: Record<string, unknown>, rejectedPath?: string) {
    mockApiCall.mockImplementation((path: string) => {
      if (path === rejectedPath) {
        return Promise.reject(new TypeError('Failed to fetch'))
      }
      if (path === '/health') {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            status: 'ok',
            supabase: true,
            stripe: true,
          }),
        })
      }
      if (path === '/admin/metrics/dashboard' && metrics) {
        return Promise.resolve({ ok: true, json: async () => metrics })
      }
      return Promise.resolve({ ok: false, json: async () => ({ detail: 'Failed to fetch dashboard metrics' }) })
    })
  }

  function card(title: string) {
    return within(screen.getByText(title).parentElement as HTMLElement)
  }

  beforeEach(() => {
    jest.clearAllMocks()
  })

  it('shows each figure from the dashboard metrics, without growth figures', async () => {
    mockResponses(METRICS)

    render(<AdminDashboard />)

    expect(await screen.findByText('Total Accounts')).toBeInTheDocument()

    expect(card('Total Accounts').getByText('1500')).toBeInTheDocument()
    expect(card('Active Subscriptions').getByText('1200')).toBeInTheDocument()
    expect(card('Running Instances').getByText('1100')).toBeInTheDocument()
    expect(card('MRR (est.)').getByText('$199')).toBeInTheDocument()
    expect(card('System Health').getByText('Supabase: ✓ Healthy | Stripe: ✓ Healthy')).toHaveClass('text-green-600')
    expect(screen.getAllByText('1500')).toHaveLength(1)
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
    expect(screen.getByText('No recent activity')).toBeInTheDocument()
  })

  it('shows a dash instead of zero for figures the metrics request did not return', async () => {
    mockResponses()

    render(<AdminDashboard />)

    expect(await screen.findByText('Operational')).toBeInTheDocument()

    for (const title of ['Total Accounts', 'Active Subscriptions', 'Running Instances', 'MRR (est.)']) {
      expect(card(title).getByText('—')).toBeInTheDocument()
    }
    expect(screen.queryByText('0')).not.toBeInTheDocument()
    expect(screen.queryByText('$0')).not.toBeInTheDocument()
    expect(screen.getByText('Recent activity unavailable')).toBeInTheDocument()
  })

  it('marks the system degraded when the health check fails', async () => {
    mockApiCall.mockResolvedValue({ ok: false })

    render(<AdminDashboard />)

    expect(await screen.findByText('Degraded')).toBeInTheDocument()

    expect(card('System Health').getByText('Supabase: ✗ Error | Stripe: ✗ Error')).toHaveClass('text-red-600')
  })

  it('keeps the health result when only the metrics request fails outright', async () => {
    mockResponses(METRICS, '/admin/metrics/dashboard')

    render(<AdminDashboard />)

    expect(await screen.findByText('Operational')).toBeInTheDocument()

    expect(card('System Health').getByText('Supabase: ✓ Healthy | Stripe: ✓ Healthy')).toBeInTheDocument()
    expect(screen.queryByText('Platform API unreachable')).not.toBeInTheDocument()
    expect(card('Total Accounts').getByText('—')).toBeInTheDocument()
    expect(screen.getByText('Recent activity unavailable')).toBeInTheDocument()
  })

  it('keeps the figures when only the health request fails outright', async () => {
    mockResponses(METRICS, '/health')

    render(<AdminDashboard />)

    expect(await screen.findByText('Degraded')).toBeInTheDocument()

    expect(card('System Health').getByText('Platform API unreachable')).toHaveClass('text-red-600')
    expect(card('Total Accounts').getByText('1500')).toBeInTheDocument()
    expect(card('MRR (est.)').getByText('$199')).toBeInTheDocument()
    expect(screen.getByText('No recent activity')).toBeInTheDocument()
    expect(logger.error).toHaveBeenCalledWith('Error fetching admin data:', expect.any(TypeError))
  })

  it('renders recent activity when the metrics response includes it', async () => {
    mockResponses({
      total_accounts: 3,
      active_subscriptions: 2,
      instances_by_status: { running: 4 },
      subscription_revenue: 199,
      recent_instances: [
        {
          created_at: '2026-05-19T17:37:00Z',
          action: 'instance_started',
          account_id: 'account_123',
        },
      ],
    })

    render(<AdminDashboard />)

    expect(await screen.findByText('instance_started')).toBeInTheDocument()

    expect(screen.getByText('account_123 - 2026-05-19T17:37:00Z')).toBeInTheDocument()
  })
})
