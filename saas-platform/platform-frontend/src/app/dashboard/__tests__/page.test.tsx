import { render, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import DashboardPage from '../page'
import { setupAccount } from '@/lib/api'
import { useAuth } from '@/hooks/useAuth'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'

jest.mock('@/hooks/useAuth', () => ({ useAuth: jest.fn() }))
jest.mock('@/hooks/useInstance', () => ({ useInstance: jest.fn() }))
jest.mock('@/hooks/useSubscription', () => ({ useSubscription: jest.fn() }))
jest.mock('@/lib/api', () => ({
  setSsoCookie: jest.fn().mockResolvedValue(undefined),
  setupAccount: jest.fn().mockResolvedValue({ message: 'Account created' }),
  provisionInstance: jest.fn(),
}))
jest.mock('@/lib/logger', () => ({ logger: { log: jest.fn(), warn: jest.fn(), error: jest.fn() } }))

describe('DashboardPage', () => {
  beforeEach(() => {
    jest.useFakeTimers()
    jest.clearAllMocks()
    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: false })
    ;(useInstance as jest.Mock).mockReturnValue({ instance: null, loading: false })
    ;(useSubscription as jest.Mock).mockReturnValue({ subscription: null, loading: false })
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  it('sets up the account once when a signed-in user has no subscription row', async () => {
    render(<DashboardPage />)

    await waitFor(() => {
      expect(setupAccount).toHaveBeenCalledTimes(1)
    })
  })
})
