import nextConfig from '../../../next.config'

describe('Next security headers', () => {
  it('does not advertise the Next.js server header', () => {
    expect(nextConfig.poweredByHeader).toBe(false)
  })

  it('sets baseline browser security headers for every page', async () => {
    const headerRules = await nextConfig.headers?.()
    const globalRule = headerRules?.find((rule) => rule.source === '/(.*)')

    expect(globalRule?.headers).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ key: 'X-Frame-Options', value: 'DENY' }),
        expect.objectContaining({ key: 'X-Content-Type-Options', value: 'nosniff' }),
        expect.objectContaining({ key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' }),
      ])
    )
  })
})
