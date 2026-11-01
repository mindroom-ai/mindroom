import { render, screen, waitFor, within } from '@testing-library/react'
import AdminDashboard from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({
  apiCall: jest.fn(),
}))

describe('AdminDashboard', () => {
  const mockApiCall = apiCall as jest.Mock

  function mockResponses(metrics: Record<string, unknown>) {
    mockApiCall.mockImplementation((path: string) => {
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
      if (path === '/admin/metrics/dashboard') {
        return Promise.resolve({ ok: true, json: async () => metrics })
      }
      return Promise.resolve({ ok: false })
    })
  }

  function card(title: string) {
    return within(screen.getByText(title).parentElement as HTMLElement)
  }

  beforeEach(() => {
    jest.clearAllMocks()
  })

  it('shows each figure from the dashboard metrics, without growth figures', async () => {
    mockResponses({
      total_accounts: 1500,
      active_subscriptions: 1200,
      instances_by_status: { running: 1100, stopped: 2 },
      subscription_revenue: 199,
      recent_instances: [],
    })

    render(<AdminDashboard />)

    await waitFor(() => {
      expect(screen.getByText('Total Accounts')).toBeInTheDocument()
    })

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
    mockApiCall.mockImplementation((path: string) =>
      Promise.resolve(
        path === '/health'
          ? { ok: true, json: async () => ({ status: 'ok', supabase: true, stripe: true }) }
          : { ok: false },
      ),
    )

    render(<AdminDashboard />)

    await waitFor(() => {
      expect(screen.getByText('Operational')).toBeInTheDocument()
    })

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

    await waitFor(() => {
      expect(screen.getByText('Degraded')).toBeInTheDocument()
    })

    expect(card('System Health').getByText('Supabase: ✗ Error | Stripe: ✗ Error')).toHaveClass('text-red-600')
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

    await waitFor(() => {
      expect(screen.getByText('instance_started')).toBeInTheDocument()
    })

    expect(screen.getByText('account_123 - 2026-05-19T17:37:00Z')).toBeInTheDocument()
  })
})
