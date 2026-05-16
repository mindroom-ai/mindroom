import { sanitizePostAuthRedirect } from '../auth/redirect'

describe('post-auth redirects', () => {
  it('allows in-app paths', () => {
    expect(sanitizePostAuthRedirect('/dashboard')).toBe('/dashboard')
  })

  it('allows HTTPS subdomains of the platform domain', () => {
    expect(sanitizePostAuthRedirect('https://1.mindroom.chat/', 'mindroom.chat')).toBe('https://1.mindroom.chat/')
  })

  it('rejects external absolute URLs', () => {
    expect(sanitizePostAuthRedirect('https://evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it('rejects protocol-relative URLs', () => {
    expect(sanitizePostAuthRedirect('//evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })
})
