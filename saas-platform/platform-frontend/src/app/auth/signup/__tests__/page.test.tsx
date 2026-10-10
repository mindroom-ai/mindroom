import { render, screen } from '@testing-library/react'
import '@testing-library/jest-dom'
import SignupPage from '../page'

jest.mock('@/components/auth/auth-shell', () => ({
  AuthShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}))
jest.mock('@/components/auth/auth-wrapper', () => ({
  AuthWrapper: ({ redirectTo }: { redirectTo?: string }) => <output aria-label="Redirect">{redirectTo ?? 'default'}</output>,
}))

describe('SignupPage', () => {
  it('opens the upgrade page on the plan picked on the landing page after signup', async () => {
    render(await SignupPage({ searchParams: Promise.resolve({ plan: 'byok' }) }))

    const redirect = new URL(screen.getByLabelText('Redirect').textContent!, 'https://app.example.com')
    expect(redirect.pathname).toBe('/auth/callback')
    expect(redirect.searchParams.get('next')).toBe('/dashboard/billing/upgrade?plan=byok')
  })

  it('keeps the default redirect without a plan', async () => {
    render(await SignupPage({ searchParams: Promise.resolve({}) }))

    expect(screen.getByLabelText('Redirect')).toHaveTextContent('default')
  })
})
