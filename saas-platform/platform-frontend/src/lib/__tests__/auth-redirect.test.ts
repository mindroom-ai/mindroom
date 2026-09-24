import { isInstanceRedirect, isPlatformRedirect, sanitizePostAuthRedirect } from '../auth/redirect'

describe('post-auth redirects', () => {
  it('allows in-app paths', () => {
    expect(sanitizePostAuthRedirect('/dashboard')).toBe('/dashboard')
    expect(sanitizePostAuthRedirect('/dashboard?tab=1#top')).toBe('/dashboard?tab=1#top')
  })

  it('keeps HTTPS subdomains of the platform domain only as candidates', () => {
    expect(sanitizePostAuthRedirect('https://1.mindroom.chat/', 'mindroom.chat')).toBe('https://1.mindroom.chat/')
    expect(isPlatformRedirect('https://1.mindroom.chat/', 'mindroom.chat')).toBe(false)
  })

  it('rejects external absolute URLs', () => {
    expect(sanitizePostAuthRedirect('https://evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it.each(['https://app.mindroom.chat./', 'https://x@app.mindroom.chat/', 'javascript:alert(1)'])(
    'rejects disguised platform target %j',
    (target) => {
      expect(sanitizePostAuthRedirect(target, 'mindroom.chat')).toBe('/dashboard')
      expect(isPlatformRedirect(target, 'mindroom.chat')).toBe(false)
    }
  )

  it('rejects protocol-relative URLs', () => {
    expect(sanitizePostAuthRedirect('//evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it('rejects backslash protocol-relative URL variants', () => {
    expect(sanitizePostAuthRedirect('/\\evil.example/phish', 'mindroom.chat')).toBe('/dashboard')
  })

  it.each(['/\t/1.mindroom.chat/', '/\n/evil.example', '/\r/evil.example', '/.//evil.example'])(
    'rejects local-looking path %j that the browser resolves off-origin',
    (target) => {
      expect(sanitizePostAuthRedirect(target, 'mindroom.chat')).toBe('/dashboard')
      expect(isPlatformRedirect(target, 'mindroom.chat')).toBe(false)
    }
  )

  it('approves only platform-operated hosts as platform redirects', () => {
    expect(isPlatformRedirect('/dashboard', 'mindroom.chat')).toBe(true)
    expect(isPlatformRedirect('https://app.mindroom.chat/dashboard', 'mindroom.chat')).toBe(true)
    expect(isPlatformRedirect('https://api.mindroom.chat/matrix-oidc/authorize?x=1', 'mindroom.chat')).toBe(true)
    expect(isPlatformRedirect('https://mindroom.chat/', 'mindroom.chat')).toBe(false)
    expect(isPlatformRedirect('https://1.api.mindroom.chat/', 'mindroom.chat')).toBe(false)
    expect(isPlatformRedirect('https://app.mindroom.chat:8443/', 'mindroom.chat')).toBe(false)
    expect(isPlatformRedirect('http://app.mindroom.chat/', 'mindroom.chat')).toBe(false)
    expect(isPlatformRedirect('https://app.mindroom.chat/', '')).toBe(false)
  })

  it('approves instance redirects only for the given instance origins', () => {
    const owned = [null, 'https://1.mindroom.chat']
    expect(isInstanceRedirect('https://1.mindroom.chat/agents?x=1', owned)).toBe(true)
    expect(isInstanceRedirect('https://2.mindroom.chat/', owned)).toBe(false)
    expect(isInstanceRedirect('https://1.mindroom.chat:8443/', owned)).toBe(false)
    expect(isInstanceRedirect('http://1.mindroom.chat/', owned)).toBe(false)
    expect(isInstanceRedirect('/dashboard', owned)).toBe(false)
    expect(isInstanceRedirect('https://1.mindroom.chat/', [])).toBe(false)
  })
})
