import { render, screen } from '@testing-library/react'
import LandingPage from '../page'

jest.mock('@/components/landing/HeroParticleBackground', () => ({
  HeroParticleBackground: () => <div data-testid="hero-particles" />,
}))

jest.mock('@/components/DarkModeToggle', () => ({
  DarkModeToggle: () => <button type="button">Toggle dark mode</button>,
}))

describe('LandingPage', () => {
  it('links to the public documentation', () => {
    render(<LandingPage />)

    const docsLinks = screen.getAllByRole('link', { name: 'Docs' })
    expect(docsLinks.length).toBeGreaterThan(0)
    expect(docsLinks[0]).toHaveAttribute('href', 'https://docs.mindroom.chat/')
  })

  it('shows the hosted BYOK, Hobby, and Pro plans', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { name: 'BYOK' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Hobby' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Pro' })).toBeInTheDocument()
    expect(screen.getByText('$10')).toBeInTheDocument()
    expect(screen.getByText('$20')).toBeInTheDocument()
    expect(screen.getByText('$200')).toBeInTheDocument()
    expect(screen.getByText('$15 included monthly AI usage')).toBeInTheDocument()
    expect(screen.getByText('$150 included monthly AI usage')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Teams' })).not.toBeInTheDocument()
  })
})
