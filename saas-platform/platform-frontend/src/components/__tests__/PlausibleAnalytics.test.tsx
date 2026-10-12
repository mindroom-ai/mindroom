import { render } from '@testing-library/react'
import { PlausibleAnalytics } from '../PlausibleAnalytics'

// next/script injects scripts after hydration; render plain elements so their content can be run here.
jest.mock('next/script', () => ({
  __esModule: true,
  default: ({ children, ...props }: { children?: string; [key: string]: unknown }) => (
    <script data-testid={props.id ?? 'external'} data-strategy={props.strategy as string} src={props.src as string | undefined}>
      {children}
    </script>
  ),
}))

type PlausibleRequest = { u: string; r?: string | null }
type PlausibleStub = { o?: { logging?: boolean; transformRequest?: (request: PlausibleRequest) => PlausibleRequest | null } }

function initOptions() {
  const { getByTestId } = render(<PlausibleAnalytics />)
  delete (window as unknown as { plausible?: unknown }).plausible
  new Function(getByTestId('plausible-init').textContent ?? '')()
  return (window as unknown as { plausible: PlausibleStub }).plausible.o
}

describe('PlausibleAnalytics', () => {
  it('loads the site Plausible script after the page is interactive', () => {
    const { getByTestId } = render(<PlausibleAnalytics />)

    const script = getByTestId('external')
    expect(script).toHaveAttribute('src', 'https://plausible.nijho.lt/js/pa-AEIwt-FWjemwLglbwP-ys.js')
    expect(script).toHaveAttribute('data-strategy', 'afterInteractive')
  })

  it('counts page views on the public pages', () => {
    const transform = initOptions()?.transformRequest

    for (const path of ['/', '/privacy', '/terms', '/auth/login', '/auth/signup?plan=hobby']) {
      const request = { u: `https://app.mindroom.chat${path}` }
      expect(transform?.(request)).toBe(request)
    }
  })

  it('never sends dashboard, admin, or other signed-in pages, even after client-side navigation', () => {
    const transform = initOptions()?.transformRequest

    for (const path of ['/dashboard', '/dashboard/billing', '/admin/accounts/3f2c9a1e-account-id', '/auth/complete', '/privacy-old']) {
      expect(transform?.({ u: `https://app.mindroom.chat${path}` })).toBeNull()
    }
  })

  it('sends only the path and campaign parameters, never other query parameters or the fragment', () => {
    const transform = initOptions()?.transformRequest

    expect(transform?.({ u: 'https://app.mindroom.chat/auth/login?redirect_to=https%3A%2F%2Fabc123.mindroom.chat%2Fsso#state' })?.u)
      .toBe('https://app.mindroom.chat/auth/login')
    expect(transform?.({ u: 'https://app.mindroom.chat/?utm_source=hn&utm_campaign=launch&session=secret' })?.u)
      .toBe('https://app.mindroom.chat/?utm_source=hn&utm_campaign=launch')
  })

  it('drops referrers from MindRoom hosts, which name hosted instances, and keeps outside ones', () => {
    const transform = initOptions()?.transformRequest

    expect(transform?.({ u: 'https://app.mindroom.chat/auth/login', r: 'https://abc123.mindroom.chat/' })?.r).toBeNull()
    expect(transform?.({ u: 'https://app.mindroom.chat/', r: 'https://mindroom.chat/' })?.r).toBeNull()
    expect(transform?.({ u: 'https://app.mindroom.chat/', r: 'https://news.ycombinator.com/' })?.r).toBe('https://news.ycombinator.com/')
  })

  it('keeps dropped events out of the browser console', () => {
    expect(initOptions()?.logging).toBe(false)
  })
})
