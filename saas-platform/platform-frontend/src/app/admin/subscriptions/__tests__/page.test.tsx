import { render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import SubscriptionsPage from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({ apiCall: jest.fn() }))
jest.mock('@/lib/logger', () => ({ logger: { error: jest.fn() } }))

const row = (id: string, tier: string, status: string) => ({
  id,
  account_id: `acc-${id}`,
  price_tier: tier,
  tier,
  status,
  price: 2000,
  billing_period: 'month',
  current_period_end: null,
  created_at: '2026-10-01T00:00:00Z',
  accounts: { email: `${id}@example.com`, full_name: null },
})

describe('SubscriptionsPage', () => {
  it('shows an account without a plan as No plan and a cancelled plan with the cancelled badge', async () => {
    ;(apiCall as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ data: [row('none', 'free', 'active'), row('lapsed', 'hobby', 'cancelled')] }),
    })

    render(<SubscriptionsPage />)

    expect(await screen.findByText('No plan')).toBeInTheDocument()
    expect(screen.queryByText('active')).not.toBeInTheDocument()
    expect(screen.getByText('cancelled')).toHaveClass('bg-red-100')
  })
})
