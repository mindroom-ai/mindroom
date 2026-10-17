import { render, screen } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import PrivacyPage from '@/app/privacy/page'
import TermsPage from '@/app/terms/page'
import RootLayout from '@/app/layout'
import { AuthShell } from '@/components/auth/auth-shell'

jest.mock('@/components/PlausibleAnalytics', () => ({
  PlausibleAnalytics: () => <div data-testid="plausible" />,
}))
jest.mock('@/components/landing/HeroParticleBackground', () => ({ HeroParticleBackground: () => null }))
jest.mock('next/font/google', () => ({
  Bricolage_Grotesque: () => ({ variable: 'font-display' }),
  Hanken_Grotesk: () => ({ variable: 'font-sans' }),
  JetBrains_Mono: () => ({ variable: 'font-mono' }),
}))

function containsType(node: ReactNode, type: unknown): boolean {
  if (!node || typeof node !== 'object') return false
  if (Array.isArray(node)) return node.some((child) => containsType(child, type))
  const element = node as ReactElement<{ children?: ReactNode }>
  return element.type === type || containsType(element.props?.children, type)
}

describe('where Plausible loads', () => {
  it('loads on the privacy and terms pages', () => {
    render(<PrivacyPage />)
    render(<TermsPage />)

    expect(screen.getAllByTestId('plausible')).toHaveLength(2)
  })

  it('loads on the sign-in and sign-up screens through their shared shell', () => {
    render(
      <AuthShell title="Welcome back" subtitle="" switchText="" switchHref="/auth/signup" switchLabel="Sign up">
        <p>form</p>
      </AuthShell>,
    )

    expect(screen.getByTestId('plausible')).toBeInTheDocument()
  })

  it('is not part of the root layout, so signed-in pages never load it', async () => {
    const { PlausibleAnalytics } = await import('@/components/PlausibleAnalytics')

    expect(containsType(RootLayout({ children: null }), PlausibleAnalytics)).toBe(false)
  })
})
