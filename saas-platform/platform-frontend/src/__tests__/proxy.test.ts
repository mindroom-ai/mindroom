/**
 * @jest-environment node
 */
import { createServerClient } from '@supabase/ssr'
import { NextRequest } from 'next/server'
import { unstable_doesMiddlewareMatch } from 'next/experimental/testing/server'
import { config, proxy } from '../proxy'

jest.mock('@supabase/ssr', () => ({ createServerClient: jest.fn() }))

const mockedCreateServerClient = createServerClient as jest.Mock

function useEnv(env: Record<string, string>) {
  const { SUPABASE_URL: _url, SUPABASE_ANON_KEY: _key, ...rest } = process.env
  jest.replaceProperty(process, 'env', { ...rest, NODE_ENV: 'production', ...env })
}

describe('proxy', () => {
  beforeEach(() => {
    useEnv({
      SUPABASE_URL: 'https://test.supabase.co',
      SUPABASE_ANON_KEY: 'test-anon-key',
      PLATFORM_DOMAIN: 'mindroom.chat',
    })
    mockedCreateServerClient.mockReturnValue({
      auth: { getUser: jest.fn().mockResolvedValue({ data: { user: null } }) },
    })
  })

  afterEach(() => {
    jest.restoreAllMocks()
    mockedCreateServerClient.mockReset()
  })

  it('limits browser connections to the API and Supabase origins', async () => {
    const response = await proxy(new NextRequest('https://app.mindroom.chat/dashboard'))
    const csp = response.headers.get('Content-Security-Policy')

    expect(csp).toContain(
      "connect-src 'self' https://api.stripe.com https://test.supabase.co wss://test.supabase.co https://api.mindroom.chat;"
    )
    expect(csp).toContain(
      "media-src 'self' https://github.com https://github-production-user-asset-6210df.s3.amazonaws.com;"
    )
    expect(response.headers.get('X-XSS-Protection')).toBeNull()
  })

  it('sends signed-out visitors of admin pages to the login page', async () => {
    const response = await proxy(new NextRequest('https://app.mindroom.chat/admin/accounts'))

    expect(response.status).toBe(307)
    expect(response.headers.get('location')).toBe(
      'https://app.mindroom.chat/auth/login?redirect_to=%2Fadmin%2Faccounts'
    )
  })

  it.each([
    ['is not an admin', () => Promise.resolve(new Response(JSON.stringify({ is_admin: false })))],
    ['cannot be checked', () => Promise.reject(new Error('API down'))],
  ])('keeps a refreshed session when the admin status %s', async (_case, adminStatus) => {
    mockedCreateServerClient.mockImplementation((_url, _key, { cookies }) => ({
      auth: {
        getUser: jest.fn(async () => {
          cookies.setAll([{ name: 'sb-test-auth-token', value: 'refreshed', options: { path: '/' } }])
          return { data: { user: { id: 'user-1' } } }
        }),
        getSession: jest.fn().mockResolvedValue({ data: { session: { access_token: 'token' } } }),
      },
    }))
    ;(global.fetch as jest.Mock).mockImplementation(adminStatus)

    const response = await proxy(new NextRequest('https://app.mindroom.chat/admin/accounts'))

    expect(response.headers.get('location')).toBe('https://app.mindroom.chat/dashboard')
    expect(response.headers.get('set-cookie')).toContain('sb-test-auth-token=refreshed; Path=/')
  })

  it('still serves pages with security headers when Supabase is not configured', async () => {
    useEnv({ PLATFORM_DOMAIN: 'mindroom.chat' })

    const response = await proxy(new NextRequest('https://app.mindroom.chat/'))

    expect(response.status).toBe(200)
    expect(response.headers.get('Content-Security-Policy')).toContain("frame-ancestors 'none'")
    expect(mockedCreateServerClient).not.toHaveBeenCalled()
  })

  it('runs for pages but not for static images', () => {
    const matches = (path: string) =>
      unstable_doesMiddlewareMatch({ config, url: `https://app.mindroom.chat${path}` })

    expect(matches('/')).toBe(true)
    expect(matches('/admin/accounts')).toBe(true)
    expect(matches('/logo.png')).toBe(false)
    expect(matches('/res/branding/mindroom.svg')).toBe(false)
    expect(matches('/_next/static/chunks/main.js')).toBe(false)
  })
})
