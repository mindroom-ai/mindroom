import { render, screen } from '@testing-library/react'
import LandingPage from '../page'

jest.mock('@/components/landing/HeroParticleBackground', () => ({
  HeroParticleBackground: () => <div data-testid="hero-particles" />,
}))

jest.mock('@/components/DarkModeToggle', () => ({
  DarkModeToggle: () => <button type="button">Toggle dark mode</button>,
}))

jest.mock('@/hooks/useDarkMode', () => ({
  useDarkMode: () => ({ isDarkMode: false }),
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

  it('leads with self-hosting and keeps hosted plans one click away', () => {
    render(<LandingPage />)

    const getStarted = screen.getAllByRole('link', { name: /Get started/ })
    expect(getStarted[0]).toHaveAttribute('href', 'https://docs.mindroom.chat/getting-started/')
    expect(screen.getByRole('link', { name: 'Try hosted MindRoom' })).toHaveAttribute('href', '#hosted')
    expect(screen.getByRole('heading', { name: 'Everything on your servers' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Prefer us to host it?' })).toBeInTheDocument()
  })

  it('shows the product film below the hero', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { name: 'Agents at work and at home.' })).toBeInTheDocument()
    expect(screen.getByLabelText('MindRoom product film')).toHaveAttribute('controls')
  })
})
