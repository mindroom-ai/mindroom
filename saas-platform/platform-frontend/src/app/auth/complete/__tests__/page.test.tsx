import { render, waitFor } from '@testing-library/react'
import { useSearchParams } from 'next/navigation'
import AuthCompletePage from '../page'
import { listInstances, setSsoCookie } from '@/lib/api'
import { navigateTo } from '@/lib/navigation'

jest.mock('@/lib/api', () => ({
  listInstances: jest.fn(),
  setSsoCookie: jest.fn(),
}))

jest.mock('@/lib/navigation', () => ({
  navigateTo: jest.fn(),
}))

async function completeWithNext(next: string | null): Promise<string> {
  ;(useSearchParams as jest.Mock).mockReturnValue({ get: (key: string) => (key === 'next' ? next : null) })
  render(<AuthCompletePage />)
  await waitFor(() => expect(navigateTo).toHaveBeenCalledTimes(1))
  return (navigateTo as jest.Mock).mock.calls[0][0]
}

describe('AuthCompletePage', () => {
  const originalConfig = window.__MINDROOM_CONFIG__

  beforeEach(() => {
    jest.clearAllMocks()
    window.__MINDROOM_CONFIG__ = { ...originalConfig!, platformDomain: 'mindroom.chat' }
    ;(setSsoCookie as jest.Mock).mockResolvedValue({ ok: true })
    ;(listInstances as jest.Mock).mockResolvedValue({
      instances: [{ frontend_url: 'https://1.mindroom.chat' }],
    })
  })

  afterEach(() => {
    window.__MINDROOM_CONFIG__ = originalConfig
  })

  it('sends another tenant host to the dashboard without minting the SSO cookie', async () => {
    expect(await completeWithNext('https://2.mindroom.chat/')).toBe('/dashboard')
    expect(listInstances).toHaveBeenCalledTimes(1)
    expect(setSsoCookie).not.toHaveBeenCalled()
  })

  it('sends the user to the dashboard without minting when the instance lookup fails', async () => {
    ;(listInstances as jest.Mock).mockRejectedValue(new Error('unauthorized'))

    expect(await completeWithNext('https://1.mindroom.chat/')).toBe('/dashboard')
    expect(setSsoCookie).not.toHaveBeenCalled()
  })

  it('mints the SSO cookie before returning to an instance the user owns', async () => {
    expect(await completeWithNext('https://1.mindroom.chat/agents')).toBe('https://1.mindroom.chat/agents')
    expect(setSsoCookie).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['/dashboard', '/dashboard'],
    [
      'https://api.mindroom.chat/matrix-oidc/authorize?client_id=synapse',
      'https://api.mindroom.chat/matrix-oidc/authorize?client_id=synapse',
    ],
    ['/\t/2.mindroom.chat/', '/dashboard'],
    [null, '/dashboard'],
  ])('mints the SSO cookie for platform destination %j without listing instances', async (next, destination) => {
    expect(await completeWithNext(next)).toBe(destination)
    expect(setSsoCookie).toHaveBeenCalledTimes(1)
    expect(listInstances).not.toHaveBeenCalled()
  })
})
