import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import InstancePage from '../page'
import { listInstances, type Instance } from '@/lib/api'
import { useInstance } from '@/hooks/useInstance'
import { useAuth } from '@/hooks/useAuth'
import { cache, instanceCache } from '@/lib/cache'
import { cacheInstance, getCachedInstance, loadInstance } from '@/lib/instance-resource'

jest.mock('@/hooks/useAuth', () => ({ useAuth: jest.fn() }))
jest.mock('@/lib/supabase/client', () => {
  const client = {}
  return { createClient: () => client }
})

jest.mock('@/lib/api', () => ({
  listInstances: jest.fn(),
  restartInstance: jest.fn(),
  startInstance: jest.fn(),
  stopInstance: jest.fn(),
}))

jest.mock('@/lib/logger', () => ({
  logger: {
    error: jest.fn(),
  },
}))

const instanceWithMissingSubdomain: Instance = {
  id: 'inst-1',
  instance_id: 1,
  subscription_id: 'sub-1',
  subdomain: null,
  status: 'error',
  frontend_url: null,
  backend_url: null,
  matrix_server_url: null,
  tier: 'enterprise',
  created_at: null,
  updated_at: null,
  kubernetes_synced_at: null,
  status_hint: null,
}

describe('InstancePage', () => {
  const originalConfig = window.__MINDROOM_CONFIG__

  beforeEach(() => {
    jest.clearAllMocks()
    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: false })
    window.__MINDROOM_CONFIG__ = {
      ...originalConfig!,
      platformDomain: 'mindroom.chat',
    }
    cache.clear()
    instanceCache.clear()
    ;(listInstances as jest.Mock).mockResolvedValue({
      instances: [instanceWithMissingSubdomain],
    })
  })

  afterEach(() => {
    window.__MINDROOM_CONFIG__ = originalConfig
    cache.clear()
    instanceCache.clear()
    jest.useRealTimers()
  })

  it('does not stringify a missing subdomain in instance details or support mailto body', async () => {
    render(<InstancePage />)

    await waitFor(() => {
      expect(screen.getByText('Instance Details')).toBeInTheDocument()
    })

    expect(screen.getAllByText('—')).toHaveLength(3)
    expect(screen.queryByText('null.mindroom.chat')).not.toBeInTheDocument()

    const supportLink = screen.getByRole('link', { name: /contact support/i })

    expect(supportLink).toHaveAttribute('href', expect.not.stringContaining('null'))
    expect(supportLink).toHaveAttribute('href', expect.stringContaining('subdomain%3A%20%E2%80%94'))
  })

  it('shows the shared cached instance while fetching fresh data', async () => {
    cacheInstance('user-1', instanceWithMissingSubdomain)
    ;(listInstances as jest.Mock).mockReturnValue(new Promise(() => {}))

    render(<InstancePage />)

    expect(screen.getByText('Instance Details')).toBeInTheDocument()
    expect(listInstances).toHaveBeenCalledTimes(1)
  })

  it('clears the shared cache when the server no longer returns an instance', async () => {
    cacheInstance('user-1', instanceWithMissingSubdomain)
    ;(listInstances as jest.Mock).mockResolvedValue({ instances: [] })

    render(<InstancePage />)

    expect(await screen.findByText('No Instance Found')).toBeInTheDocument()
    expect(getCachedInstance('user-1')).toBeNull()
  })

  it('expires cached instances after fifteen seconds', () => {
    jest.useFakeTimers()
    cacheInstance('user-1', instanceWithMissingSubdomain)
    jest.advanceTimersByTime(15001)
    ;(listInstances as jest.Mock).mockReturnValue(new Promise(() => {}))

    render(<InstancePage />)

    expect(screen.queryByText('Instance Details')).not.toBeInTheDocument()
    expect(listInstances).toHaveBeenCalledTimes(1)
  })

  it('polls transitional instances every five seconds and stops when running', async () => {
    jest.useFakeTimers()
    ;(listInstances as jest.Mock)
      .mockResolvedValueOnce({
        instances: [{ ...instanceWithMissingSubdomain, status: 'provisioning' }],
      })
      .mockResolvedValue({ instances: [{ ...instanceWithMissingSubdomain, status: 'running' }] })

    render(<InstancePage />)
    await act(async () => {})
    expect(screen.getByText('Setting up your MindRoom instance... This may take a few minutes.')).toBeInTheDocument()

    await act(async () => { jest.advanceTimersByTime(4999) })
    expect(listInstances).toHaveBeenCalledTimes(1)
    await act(async () => { jest.advanceTimersByTime(1) })
    expect(listInstances).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Instance is running and accessible')).toBeInTheDocument()
    await act(async () => { jest.advanceTimersByTime(10000) })
    expect(listInstances).toHaveBeenCalledTimes(2)
  })

  it('shows loading during manual refresh and then renders the fresh result', async () => {
    render(<InstancePage />)
    await screen.findByText('Instance Details')
    let finishRefresh!: (value: { instances: typeof instanceWithMissingSubdomain[] }) => void
    ;(listInstances as jest.Mock).mockReturnValue(
      new Promise(resolve => { finishRefresh = resolve })
    )

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(screen.queryByText('Instance Details')).not.toBeInTheDocument()

    await act(async () => { finishRefresh({ instances: [] }) })
    expect(screen.getByText('No Instance Found')).toBeInTheDocument()
    expect(getCachedInstance('user-1')).toBeNull()
  })
  it('shares a hook-loaded instance with the detail page during background refresh', async () => {
    const hook = renderHook(() => useInstance())
    await waitFor(() => { expect(hook.result.current.loading).toBe(false) })
    expect(hook.result.current.instance).toEqual(instanceWithMissingSubdomain)
    hook.unmount()

    ;(listInstances as jest.Mock).mockReturnValue(new Promise(() => {}))
    render(<InstancePage />)

    expect(screen.getByText('Instance Details')).toBeInTheDocument()
    expect(listInstances).toHaveBeenCalledTimes(2)
  })

  it('waits for authentication before fetching and polls the hook every fifteen seconds', async () => {
    jest.useFakeTimers()
    ;(useAuth as jest.Mock).mockReturnValue({ user: null, loading: true })
    const hook = renderHook(() => useInstance())
    expect(listInstances).not.toHaveBeenCalled()

    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: false })
    await act(async () => { hook.rerender() })
    expect(listInstances).toHaveBeenCalledTimes(1)
    expect(hook.result.current.loading).toBe(false)

    await act(async () => { jest.advanceTimersByTime(14999) })
    expect(listInstances).toHaveBeenCalledTimes(1)
    ;(listInstances as jest.Mock).mockResolvedValue({ instances: [] })
    await act(async () => { jest.advanceTimersByTime(1) })
    expect(listInstances).toHaveBeenCalledTimes(2)
    expect(hook.result.current.instance).toBeNull()
    expect(getCachedInstance('user-1')).toBeNull()

    hook.unmount()
    await act(async () => { jest.advanceTimersByTime(15000) })
    expect(listInstances).toHaveBeenCalledTimes(2)
  })

  it('does not show a previous account instance after an account switch', async () => {
    const page = render(<InstancePage />)
    await screen.findByText('Instance Details')

    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-2' }, loading: false })
    ;(listInstances as jest.Mock).mockReturnValue(new Promise(() => {}))
    page.rerender(<InstancePage />)

    expect(screen.queryByText('Instance Details')).not.toBeInTheDocument()
    expect(getCachedInstance('user-2')).toBeNull()
  })

  it('keeps a late previous-account hook response out of the current account', async () => {
    let finishFirst!: (value: { instances: typeof instanceWithMissingSubdomain[] }) => void
    ;(listInstances as jest.Mock).mockReturnValueOnce(
      new Promise(resolve => { finishFirst = resolve })
    )
    const hook = renderHook(() => useInstance())
    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-2' }, loading: false })
    ;(listInstances as jest.Mock).mockResolvedValue({ instances: [] })
    await act(async () => { hook.rerender() })
    expect(hook.result.current.instance).toBeNull()

    await act(async () => { finishFirst({ instances: [instanceWithMissingSubdomain] }) })
    expect(hook.result.current.instance).toBeNull()
    expect(getCachedInstance('user-2')).toBeNull()
  })

  it('does not let a late load replace a newer cached result, including an empty result', async () => {
    let finishFirst!: (value: { instances: typeof instanceWithMissingSubdomain[] }) => void
    ;(listInstances as jest.Mock)
      .mockReturnValueOnce(new Promise(resolve => { finishFirst = resolve }))
      .mockResolvedValueOnce({ instances: [] })
    const first = loadInstance('user-1')
    await loadInstance('user-1')
    finishFirst({ instances: [instanceWithMissingSubdomain] })
    await first

    expect(getCachedInstance('user-1')).toBeNull()
  })

  it('retains the current instance during a same-account auth refresh after cache expiry', async () => {
    jest.useFakeTimers()
    const hook = renderHook(() => useInstance())
    await act(async () => {})
    expect(hook.result.current.instance).toEqual(instanceWithMissingSubdomain)

    jest.setSystemTime(Date.now() + 16000)
    let failRefresh!: (error: Error) => void
    ;(listInstances as jest.Mock).mockReturnValue(
      new Promise((_, reject) => { failRefresh = reject })
    )
    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: false })
    await act(async () => { hook.rerender() })

    expect(hook.result.current.instance).toEqual(instanceWithMissingSubdomain)
    expect(hook.result.current.loading).toBe(false)
    await act(async () => { failRefresh(new Error('refresh failed')) })
    expect(hook.result.current.instance).toEqual(instanceWithMissingSubdomain)
    expect(hook.result.current.loading).toBe(false)
  })

  it('starts transitional polling when authentication becomes ready without a status change', async () => {
    jest.useFakeTimers()
    const provisioning: Instance = { ...instanceWithMissingSubdomain, status: 'provisioning' }
    cacheInstance('user-1', provisioning)
    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: true })
    ;(listInstances as jest.Mock).mockResolvedValue({ instances: [provisioning] })
    const page = render(<InstancePage />)
    expect(listInstances).not.toHaveBeenCalled()

    ;(useAuth as jest.Mock).mockReturnValue({ user: { id: 'user-1' }, loading: false })
    await act(async () => { page.rerender(<InstancePage />) })
    expect(listInstances).toHaveBeenCalledTimes(1)
    await act(async () => { jest.advanceTimersByTime(5000) })
    expect(listInstances).toHaveBeenCalledTimes(2)
  })

})
