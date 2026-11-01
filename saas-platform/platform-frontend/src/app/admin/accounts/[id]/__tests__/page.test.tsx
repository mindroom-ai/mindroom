import { render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import AccountDetailsPage from '../page'
import { apiCall } from '@/lib/api'

jest.mock('@/lib/api', () => ({ apiCall: jest.fn() }))
jest.mock('@/lib/logger', () => ({ logger: { error: jest.fn() } }))
jest.mock('next/navigation', () => ({
  useParams: () => ({ id: 'acc-1' }),
  useRouter: () => ({ push: jest.fn() }),
}))

const details = (subscription: Record<string, unknown>) => ({
  account: { id: 'acc-1', email: 'owner@example.com', status: 'active', is_admin: false },
  subscription: { id: 'sub-1', status: 'active', ...subscription },
  instances: [],
})

describe('AccountDetailsPage', () => {
  it.each([
    ['its recorded Stripe price', { tier: 'byok', price_cents: 9600, billing_cycle: 'yearly' }, 'byok - $96/year'],
    ['only its tier before a price is recorded', { tier: 'byok', price_cents: null, billing_cycle: null }, 'byok'],
  ])('shows a subscription with %s', async (_case, subscription, shown) => {
    ;(apiCall as jest.Mock).mockResolvedValue({ ok: true, json: async () => details(subscription) })

    render(<AccountDetailsPage />)

    expect(await screen.findByText(shown)).toBeInTheDocument()
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()
  })

  it('offers no account actions, which live on the accounts list', async () => {
    ;(apiCall as jest.Mock).mockResolvedValue({ ok: true, json: async () => details({ tier: 'free' }) })

    render(<AccountDetailsPage />)

    expect(await screen.findByText('owner@example.com')).toBeInTheDocument()
    expect(screen.getAllByRole('button').map(button => button.textContent)).toEqual(['Back to Accounts'])
  })
})
