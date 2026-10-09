import { sanitizePostAuthRedirect } from '../auth/redirect'

describe('post-auth redirects', () => {
  it('allows in-app paths', () => {
    expect(sanitizePostAuthRedirect('/dashboard')).toBe('/dashboard')
  })

  it.each([
    'https://app.mindroom.chat/dashboard',
    'https://api.mindroom.chat/instance-sso/authorize?redirect_to=https%3A%2F%2F1.mindroom.chat%2F',
    'https://api.mindroom.chat/matrix-oidc/authorize?client_id=mindroom-synapse',
  ])('allows the platform app and API hosts (%s)', (target) => {
    expect(sanitizePostAuthRedirect(target, 'mindroom.chat')).toBe(target)
  })

  it.each(['https://1.mindroom.chat/', 'https://1.matrix.mindroom.chat/', 'https://mindroom.chat/'])(
    'rejects tenant instance and other platform-domain hosts (%s)',
    (target) => {
      expect(sanitizePostAuthRedirect(target, 'mindroom.chat')).toBe('/dashboard')
    }
  )

  it('rejects external absolute URLs', () => {
    expect(sanitizePostAuthRedirect('https://evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it('rejects protocol-relative URLs', () => {
    expect(sanitizePostAuthRedirect('//evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it('rejects backslash protocol-relative URL variants', () => {
    expect(sanitizePostAuthRedirect('/\\evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it.each(['/\t/evil.example', '/\n/evil.example', '/\r/evil.example'])(
    'rejects targets whose control characters browsers strip (%j)',
    (target) => {
      expect(sanitizePostAuthRedirect(target, 'mindroom.chat')).toBe('/dashboard')
    }
  )
})
