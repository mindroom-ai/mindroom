import { render } from '@testing-library/react'
import { PlausibleAnalytics } from '../PlausibleAnalytics'

// next/script injects scripts after hydration; render plain elements so the configuration is visible.
jest.mock('next/script', () => ({
  __esModule: true,
  default: ({ children, ...props }: { children?: string; [key: string]: unknown }) => (
    <script data-testid={props.id ?? 'external'} data-strategy={props.strategy as string} src={props.src as string | undefined}>
      {children}
    </script>
  ),
}))

describe('PlausibleAnalytics', () => {
  it('loads the site Plausible script after the page is interactive', () => {
    const { getByTestId } = render(<PlausibleAnalytics />)

    const script = getByTestId('external')
    expect(script).toHaveAttribute('src', 'https://plausible.nijho.lt/js/pa-AEIwt-FWjemwLglbwP-ys.js')
    expect(script).toHaveAttribute('data-strategy', 'afterInteractive')
  })

  it('queues calls until the script loads and then initializes it', () => {
    const { getByTestId } = render(<PlausibleAnalytics />)

    const init = getByTestId('plausible-init')
    expect(init.textContent).toContain('window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)}')
    expect(init.textContent).toContain('plausible.init()')
  })
})
