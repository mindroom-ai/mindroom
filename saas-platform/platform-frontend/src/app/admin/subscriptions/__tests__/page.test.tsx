import { render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom'
import SubscriptionsPage from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({ apiCall: jest.fn() }))
jest.mock('@/lib/logger', () => ({ logger: { error: jest.fn() } }))

const row = (id: string, tier: string, status: string, price_cents: number | null = 2000, billing_cycle = 'monthly') => ({
  id,
  account_id: `acc-${id}`,
  tier,
  status,
  price_cents,
  billing_cycle,
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
    // The admin portal has no subscription action to offer.
    expect(screen.queryAllByRole('button')).toEqual([])
  })

  it("shows each subscription's recorded Stripe price, and a dash before one is recorded", async () => {
    ;(apiCall as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ data: [row('yearly', 'byok', 'active', 9600, 'yearly'), row('older', 'hobby', 'active', null)] }),
    })

    render(<SubscriptionsPage />)

    const yearly = (await screen.findByText('yearly@example.com')).closest('tr') as HTMLElement
    expect(within(yearly).getByText('$96/year')).toBeInTheDocument()
    const older = screen.getByText('older@example.com').closest('tr') as HTMLElement
    expect(within(older).getByText('—')).toBeInTheDocument()
    expect(within(older).queryByText(/\$/)).not.toBeInTheDocument()
  })
})
