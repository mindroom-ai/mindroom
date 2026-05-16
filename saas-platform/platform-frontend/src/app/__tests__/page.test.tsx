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
})
