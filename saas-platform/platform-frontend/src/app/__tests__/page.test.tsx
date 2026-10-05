import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

  it('shows the hosted own-keys, Hobby, and Pro plans', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { name: 'Your own keys' })).toBeInTheDocument()
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

  it('shows the product film with the use cases', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { name: "Your assistant at home, your team's memory at work." })).toBeInTheDocument()
    expect(screen.getByLabelText('MindRoom product film')).toHaveAttribute('controls')
  })

  it('copies the install command', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<LandingPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }))

    expect(writeText).toHaveBeenCalledWith('uvx mindroom run')
    const copied = await screen.findByRole('button', { name: 'Copied' })
    expect(copied).toHaveTextContent('Copied')
  })

  it('switches the install card to the macOS app', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<LandingPage />)

    fireEvent.click(screen.getByRole('button', { name: 'macOS app' }))

    expect(screen.getByRole('button', { name: 'macOS app' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('mindroom-ai/tap/mindroom').closest('pre')).toHaveTextContent('$ brew install --cask mindroom-ai/tap/mindroom')
    expect(screen.getByRole('link', { name: /Read the macOS app guide/ })).toHaveAttribute('href', 'https://docs.mindroom.chat/installation/macos-app/')
    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }))
    expect(writeText).toHaveBeenCalledWith('brew install --cask mindroom-ai/tap/mindroom')
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('selects the install command when the clipboard is refused', async () => {
    const writeText = jest.fn().mockRejectedValue(new DOMException('Denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<LandingPage />)

    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }))

    await waitFor(() => expect(window.getSelection()?.toString()).toBe('uvx mindroom run'))
    expect(screen.getByRole('button', { name: 'Copy command' })).toHaveTextContent('Copy')
  })

  it('opens a menu with every page link and closes it on Escape', () => {
    render(<LandingPage />)
    const button = screen.getByRole('button', { name: 'Menu' })

    fireEvent.click(button)

    expect(button).toHaveAttribute('aria-expanded', 'true')
    const menuElement = document.getElementById(button.getAttribute('aria-controls') ?? '')
    expect(menuElement).not.toBeNull()
    const menu = within(menuElement as HTMLElement)
    expect(menu.getAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['Open MindRoom Chat', 'https://chat.mindroom.chat'],
      ['Why MindRoom', '#why'],
      ['Self-host', '#self-host'],
      ['Hosted', '#hosted'],
      ['Showcase', 'https://docs.mindroom.chat/showcase/'],
      ['Docs', 'https://docs.mindroom.chat/'],
      ['GitHub', 'https://github.com/mindroom-ai/mindroom'],
      ['Sign in', '/auth/login'],
    ])

    fireEvent.keyDown(document, { key: 'Escape' })

    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(button).toHaveFocus()
    expect(document.getElementById('mobile-menu')).toBeNull()
  })

  it('closes the menu when keyboard focus moves outside it', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))

    fireEvent.focusIn(screen.getByRole('link', { name: 'Try hosted MindRoom' }))

    expect(document.getElementById('mobile-menu')).toBeNull()
  })

  it('keeps the menu open for focus and taps inside it', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))
    const menuElement = document.getElementById('mobile-menu') as HTMLElement
    const menu = within(menuElement)

    fireEvent.focusIn(menu.getByRole('link', { name: 'GitHub' }))
    fireEvent.pointerDown(menuElement)
    fireEvent.blur(menu.getByRole('link', { name: 'GitHub' }), { relatedTarget: null })

    expect(document.getElementById('mobile-menu')).not.toBeNull()
  })

  it('closes the menu after following a link', () => {
    render(<LandingPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Menu' }))

    fireEvent.click(within(document.getElementById('mobile-menu') as HTMLElement).getByRole('link', { name: 'Hosted' }))

    expect(document.getElementById('mobile-menu')).toBeNull()
  })

  it('links to MindRoom Chat from the header and the hero', () => {
    render(<LandingPage />)

    expect(screen.getByRole('link', { name: 'Open Chat' })).toHaveAttribute('href', 'https://chat.mindroom.chat')
    expect(screen.getByRole('link', { name: 'Open MindRoom Chat' })).toHaveAttribute('href', 'https://chat.mindroom.chat')
  })

  it('lists the MindRoom Chat apps', () => {
    render(<LandingPage />)

    const apps = within(screen.getByRole('list', { name: 'MindRoom Chat apps' }))
    expect(apps.getByRole('link', { name: 'Web' })).toHaveAttribute('href', 'https://chat.mindroom.chat')
    expect(apps.getByRole('link', { name: 'Mac' })).toHaveAttribute('href', 'https://docs.mindroom.chat/installation/macos-app/')
    expect(apps.getByRole('link', { name: 'iPhone & iPad' })).toHaveAttribute('href', 'https://apps.apple.com/us/app/mindroom-ai/id6760272172')
    expect(apps.getByText('Android')).toHaveTextContent('AndroidBeta')
  })
})
