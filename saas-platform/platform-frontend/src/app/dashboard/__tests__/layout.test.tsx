import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { AuthSessionMissingError, type AuthChangeEvent, type Session, type User, type UserResponse } from '@supabase/supabase-js'
import { useRouter } from 'next/navigation'
import DashboardLayout from '../layout'
import { useAuth } from '@/hooks/useAuth'
import { DarkModeProvider } from '@/hooks/useDarkMode'
import { useInstance } from '@/hooks/useInstance'
import { useSubscription } from '@/hooks/useSubscription'
import { useUsage } from '@/hooks/useUsage'
import { apiCall, clearSsoCookie, listInstances } from '@/lib/api'
import { cache, instanceCache, subscriptionCache } from '@/lib/cache'
import { createClient } from '@/lib/supabase/client'

jest.mock('@/lib/supabase/client', () => ({ createClient: jest.fn() }))
jest.mock('@/lib/api', () => ({
  apiCall: jest.fn(),
  clearSsoCookie: jest.fn(),
  listInstances: jest.fn(),
}))

type AuthListener = (event: AuthChangeEvent, session: Session | null) => void

const user: User = {
  id: 'user-123',
  email: 'reader@example.com',
  app_metadata: {},
  user_metadata: {},
  aud: 'authenticated',
  created_at: '2026-01-01T00:00:00Z',
}

function DashboardConsumers() {
  const { user, loading } = useAuth()
  const { loading: instanceLoading } = useInstance()
  const { loading: subscriptionLoading } = useSubscription()
  const { loading: usageLoading } = useUsage()

  return (
    <>
      <output aria-label="Current user">{loading ? 'Loading' : user?.email}</output>
      <output aria-label="Dashboard data">
        {instanceLoading || subscriptionLoading || usageLoading ? 'Loading' : 'Ready'}
      </output>
    </>
  )
}

function dashboard(children = <DashboardConsumers />) {
  return (
    <DarkModeProvider>
      <DashboardLayout>{children}</DashboardLayout>
    </DarkModeProvider>
  )
}

describe('dashboard authentication', () => {
  const router = {
    push: jest.fn(),
    replace: jest.fn(),
    refresh: jest.fn(),
    back: jest.fn(),
    forward: jest.fn(),
    prefetch: jest.fn(),
  }
  const listeners = new Set<AuthListener>()
  const getUser = jest.fn<Promise<UserResponse>, []>()
  const signOut = jest.fn()
  const unsubscribe = jest.fn()
  const onAuthStateChange = jest.fn((listener: AuthListener) => {
    listeners.add(listener)
    return {
      data: {
        subscription: {
          unsubscribe: () => {
            listeners.delete(listener)
            unsubscribe()
          },
        },
      },
    }
  })

  beforeEach(() => {
    jest.clearAllMocks()
    listeners.clear()
    cache.clear()
    instanceCache.clear()
    subscriptionCache.clear()
    jest.mocked(useRouter).mockReturnValue(router)
    jest.mocked(createClient).mockReturnValue({
      auth: { getUser, signOut, onAuthStateChange },
    } as unknown as ReturnType<typeof createClient>)
    getUser.mockResolvedValue({ data: { user }, error: null })
    signOut.mockResolvedValue({ error: null })
    jest.mocked(clearSsoCookie).mockResolvedValue(undefined)
    jest.mocked(listInstances).mockResolvedValue({ instances: [] })
    jest.mocked(apiCall).mockImplementation(async (endpoint) => {
      if (endpoint === '/my/subscription') {
        return new Response(null, { status: 404 })
      }
      return new Response(JSON.stringify({
        aggregated: { totalMessages: 0, totalAgents: 0, totalStorage: 0 },
        usage: [],
      }))
    })
  })

  it('shares one lookup and listener across the gate, chrome, and data hooks', async () => {
    const initialUser = Promise.withResolvers<UserResponse>()
    getUser.mockReturnValue(initialUser.promise)
    const { rerender, unmount } = render(dashboard())

    expect(screen.queryByLabelText('Current user')).not.toBeInTheDocument()
    expect(listInstances).not.toHaveBeenCalled()
    expect(router.push).not.toHaveBeenCalled()

    await act(async () => initialUser.resolve({ data: { user }, error: null }))
    await waitFor(() => expect(screen.getByLabelText('Dashboard data')).toHaveTextContent('Ready'))
    expect(screen.getAllByText('reader@example.com')).toHaveLength(2)
    expect(getUser).toHaveBeenCalledTimes(1)
    expect(onAuthStateChange).toHaveBeenCalledTimes(1)
    expect(listeners.size).toBe(1)

    const updatedUser = { ...user, email: 'updated@example.com' }
    act(() => {
      for (const listener of listeners) {
        listener('USER_UPDATED', {
          access_token: 'test-access-token',
          refresh_token: 'test-refresh-token',
          expires_in: 3600,
          token_type: 'bearer',
          user: updatedUser,
        })
      }
    })
    await waitFor(() => expect(screen.getByLabelText('Dashboard data')).toHaveTextContent('Ready'))
    expect(screen.getAllByText('updated@example.com')).toHaveLength(2)

    rerender(dashboard(<p>Another dashboard page</p>))
    expect(getUser).toHaveBeenCalledTimes(1)
    expect(onAuthStateChange).toHaveBeenCalledTimes(1)
    expect(unsubscribe).not.toHaveBeenCalled()

    unmount()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
    expect(listeners.size).toBe(0)
  })

  it('redirects an unauthenticated visitor without mounting dashboard consumers', async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: new AuthSessionMissingError() })
    render(dashboard())

    await waitFor(() => expect(router.push).toHaveBeenCalledWith('/auth/login'))
    expect(screen.queryByLabelText('Current user')).not.toBeInTheDocument()
    expect(listInstances).not.toHaveBeenCalled()
    expect(apiCall).not.toHaveBeenCalled()
  })

  it('hides dashboard content and preserves both redirects after SIGNED_OUT', async () => {
    render(dashboard())
    await waitFor(() => expect(screen.getByLabelText('Dashboard data')).toHaveTextContent('Ready'))

    act(() => {
      for (const listener of listeners) listener('SIGNED_OUT', null)
    })

    expect(screen.queryByLabelText('Current user')).not.toBeInTheDocument()
    expect(router.push.mock.calls).toEqual([['/'], ['/auth/login']])
  })

  it.each(['success', 'failure'])('waits for SSO cookie cleanup before sign-out on %s', async (outcome) => {
    const cleanup = Promise.withResolvers<void>()
    jest.mocked(clearSsoCookie).mockReturnValue(cleanup.promise)
    render(dashboard())
    await waitFor(() => expect(screen.getByLabelText('Dashboard data')).toHaveTextContent('Ready'))

    fireEvent.click(screen.getAllByRole('button', { name: 'Sign out' })[0])
    expect(clearSsoCookie).toHaveBeenCalledTimes(1)
    expect(signOut).not.toHaveBeenCalled()

    await act(async () => {
      if (outcome === 'success') cleanup.resolve()
      else cleanup.reject(new Error('Cookie cleanup failed'))
    })

    expect(signOut).toHaveBeenCalledTimes(1)
    expect(screen.getAllByRole('button', { name: 'Sign out' })[0]).toBeEnabled()
  })
})
