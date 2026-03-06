import { getServerRuntimeConfig } from '../runtime-config'

describe('runtime config', () => {
  const originalSupabaseUrl = process.env.SUPABASE_URL
  const originalSupabaseAnonKey = process.env.SUPABASE_ANON_KEY
  const originalPlatformDomain = process.env.PLATFORM_DOMAIN

  afterEach(() => {
    if (originalSupabaseUrl === undefined) {
      delete process.env.SUPABASE_URL
    } else {
      process.env.SUPABASE_URL = originalSupabaseUrl
    }

    if (originalSupabaseAnonKey === undefined) {
      delete process.env.SUPABASE_ANON_KEY
    } else {
      process.env.SUPABASE_ANON_KEY = originalSupabaseAnonKey
    }

    if (originalPlatformDomain === undefined) {
      delete process.env.PLATFORM_DOMAIN
    } else {
      process.env.PLATFORM_DOMAIN = originalPlatformDomain
    }
  })

  it('allows optional runtime config without Supabase env vars', () => {
    delete process.env.SUPABASE_URL
    delete process.env.SUPABASE_ANON_KEY
    process.env.PLATFORM_DOMAIN = 'platform.local'

    expect(getServerRuntimeConfig({ requireSupabase: false })).toEqual(
      expect.objectContaining({
        supabaseUrl: '',
        supabaseAnonKey: '',
        platformDomain: 'platform.local',
      })
    )
  })

  it('still requires Supabase env vars for auth-sensitive server paths', () => {
    delete process.env.SUPABASE_URL
    delete process.env.SUPABASE_ANON_KEY

    expect(() => getServerRuntimeConfig()).toThrow('SUPABASE_URL must be provided at runtime')
  })
})
