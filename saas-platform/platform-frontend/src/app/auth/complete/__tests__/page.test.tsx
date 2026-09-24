import { act, render } from '@testing-library/react'
import { useSearchParams } from 'next/navigation'
import AuthCompletePage from '../page'
import { listInstances, setSsoCookie } from '@/lib/api'

jest.mock('@/lib/api', () => ({
  listInstances: jest.fn(),
  setSsoCookie: jest.fn(),
}))

// jsdom cannot observe `window.location.href` navigation, but the same approval gates the SSO cookie.
async function completeWithNext(next: string | null) {
  ;(useSearchParams as jest.Mock).mockReturnValue({ get: (key: string) => (key === 'next' ? next : null) })
  render(<AuthCompletePage />)
  await act(() => new Promise((resolve) => setTimeout(resolve, 0)))
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

  it('does not mint the SSO cookie for another tenant host', async () => {
    await completeWithNext('https://2.mindroom.chat/')

    expect(listInstances).toHaveBeenCalledTimes(1)
    expect(setSsoCookie).not.toHaveBeenCalled()
  })

  it('does not mint the SSO cookie when the instance lookup fails', async () => {
    ;(listInstances as jest.Mock).mockRejectedValue(new Error('unauthorized'))
    await completeWithNext('https://1.mindroom.chat/')

    expect(setSsoCookie).not.toHaveBeenCalled()
  })

  it('mints the SSO cookie for an instance the user owns', async () => {
    await completeWithNext('https://1.mindroom.chat/agents')

    expect(listInstances).toHaveBeenCalledTimes(1)
    expect(setSsoCookie).toHaveBeenCalledTimes(1)
  })

  it.each([
    '/dashboard',
    'https://api.mindroom.chat/matrix-oidc/authorize?client_id=synapse',
    '/\t/2.mindroom.chat/',
    null,
  ])('mints the SSO cookie for platform destination %j without listing instances', async (next) => {
    await completeWithNext(next)

    expect(setSsoCookie).toHaveBeenCalledTimes(1)
    expect(listInstances).not.toHaveBeenCalled()
  })
})
