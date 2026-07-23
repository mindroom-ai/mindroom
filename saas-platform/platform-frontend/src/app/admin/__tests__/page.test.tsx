import { render, screen, waitFor } from '@testing-library/react'
import AdminDashboard from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({
  apiCall: jest.fn(),
}))

describe('AdminDashboard', () => {
  const mockApiCall = apiCall as jest.Mock

  beforeEach(() => {
    jest.clearAllMocks()
  })

  it('renders partial metrics responses without crashing', async () => {
    mockApiCall.mockImplementation((path: string) => {
      if (path === '/admin/stats') {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            accounts: 3,
            active_subscriptions: 2,
            running_instances: 1,
          }),
        })
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
      return Promise.resolve({
        ok: true,
        json: async () => ({
          total_accounts: 3,
          active_subscriptions: 2,
          subscription_revenue: 199,
        }),
      })
    })

    render(<AdminDashboard />)

    await waitFor(() => {
      expect(screen.getByText('No recent activity')).toBeInTheDocument()
    })

    expect(screen.getByText('$199')).toBeInTheDocument()
  })

  it('renders recent activity when the metrics response includes it', async () => {
    mockApiCall.mockImplementation((path: string) => {
      if (path === '/admin/stats') {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            accounts: 3,
            active_subscriptions: 2,
            running_instances: 1,
          }),
        })
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
      return Promise.resolve({
        ok: true,
        json: async () => ({
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
        }),
      })
    })

    render(<AdminDashboard />)

    await waitFor(() => {
      expect(screen.getByText('instance_started')).toBeInTheDocument()
    })

    expect(screen.getByText('account_123 - 2026-05-19T17:37:00Z')).toBeInTheDocument()
  })
})
