import { render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom'
import InstancePage from '../page'
import { listInstances } from '@/lib/api'
import { cache } from '@/lib/cache'

jest.mock('@/lib/api', () => ({
  listInstances: jest.fn(),
  restartInstance: jest.fn(),
  startInstance: jest.fn(),
  stopInstance: jest.fn(),
}))

jest.mock('@/lib/cache', () => ({
  cache: {
    get: jest.fn(),
    set: jest.fn(),
  },
}))

jest.mock('@/lib/logger', () => ({
  logger: {
    error: jest.fn(),
  },
}))

const instanceWithMissingSubdomain = {
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
    window.__MINDROOM_CONFIG__ = {
      ...originalConfig!,
      platformDomain: 'mindroom.chat',
    }
    ;(cache.get as jest.Mock).mockReturnValue(null)
    ;(listInstances as jest.Mock).mockResolvedValue({
      instances: [instanceWithMissingSubdomain],
    })
  })

  afterEach(() => {
    window.__MINDROOM_CONFIG__ = originalConfig
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
})
