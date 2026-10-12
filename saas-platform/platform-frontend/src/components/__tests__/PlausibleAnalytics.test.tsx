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

type PlausibleStub = { o?: { transformRequest?: (request: { u: string }) => { u: string } | null } }

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
})
