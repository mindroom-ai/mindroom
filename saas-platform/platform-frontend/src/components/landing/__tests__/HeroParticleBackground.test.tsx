import { render, screen } from '@testing-library/react'

jest.mock('@basnijholt/particular-drift/react', () => ({
  ParticularDriftCanvas: ({ className, imageUrl, options }: {
    className?: string
    imageUrl: string
    options: { particleColor: string; particleCount: number }
  }) => (
    <canvas
      className={className}
      data-image-url={imageUrl}
      data-particle-color={options.particleColor}
      data-particle-count={String(options.particleCount)}
      data-testid="particle-canvas"
    />
  ),
}), { virtual: true })

jest.mock('@/hooks/useDarkMode', () => ({
  useDarkMode: jest.fn(),
}))

import { useDarkMode } from '@/hooks/useDarkMode'
import { HeroParticleBackground } from '../HeroParticleBackground'

beforeEach(() => {
  ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: true })
})

describe('HeroParticleBackground', () => {
  it('renders a scoped MindRoom logo particle canvas for the landing hero', () => {
    render(<HeroParticleBackground />)

    const background = screen.getByTestId('landing-particle-background')
    const canvas = screen.getByTestId('particle-canvas')

    expect(background).toHaveAttribute('aria-hidden', 'true')
    expect(background).toHaveClass('absolute')
    expect(background).toHaveClass('block')
    expect(background).toHaveClass('top-80')
    expect(background).toHaveClass('lg:w-[70%]')
    expect(background).not.toHaveClass('hidden')
    expect(canvas).toHaveAttribute('data-image-url', '/res/branding/mindroom.svg')
    expect(canvas).toHaveAttribute('data-particle-color', '#dda290')
  })

  it('uses the light MindRoom Chat palette in light mode, except on the dark auth screen', () => {
    ;(useDarkMode as jest.Mock).mockReturnValue({ isDarkMode: false })
    const { rerender } = render(<HeroParticleBackground />)

    expect(screen.getByTestId('landing-particle-background').style.getPropertyValue('--particle-backdrop')).toBe('#f6e8d6')
    expect(screen.getByTestId('particle-canvas')).toHaveAttribute('data-particle-color', '#5636a3')

    rerender(<HeroParticleBackground variant="auth" />)

    expect(screen.getByTestId('particle-canvas')).toHaveAttribute('data-particle-color', '#dda290')
  })

  it('can fill the auth screen with the MindRoom particle field', () => {
    render(<HeroParticleBackground variant="auth" />)

    const background = screen.getByTestId('landing-particle-background')
    const canvas = screen.getByTestId('particle-canvas')

    expect(background).toHaveAttribute('data-variant', 'auth')
    expect(background).toHaveClass('fixed')
    expect(background).toHaveClass('inset-0')
    expect(background).toHaveClass('bg-[#0f0d2e]')
    expect(background).not.toHaveClass('top-80')
    expect(background).not.toHaveClass('lg:w-[70%]')
    expect(canvas).toHaveClass('opacity-90')
  })

  it.each([
    { hardwareConcurrency: 4, devicePixelRatio: 1, width: 1440, height: 900, coarsePointer: false, expected: '9000' },
    { hardwareConcurrency: 8, devicePixelRatio: 1, width: 1440, height: 900, coarsePointer: false, expected: '20000' },
    { hardwareConcurrency: 12, devicePixelRatio: 2, width: 1440, height: 900, coarsePointer: false, expected: '20000' },
    { hardwareConcurrency: 12, devicePixelRatio: 1, width: 1440, height: 900, coarsePointer: false, expected: '32000' },
  ])(
    'selects $expected particles for the device profile',
    ({ hardwareConcurrency, devicePixelRatio, width, height, coarsePointer, expected }) => {
      Object.defineProperty(window.navigator, 'hardwareConcurrency', {
        configurable: true,
        value: hardwareConcurrency,
      })
      Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: devicePixelRatio })
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
      window.matchMedia = jest.fn().mockReturnValue({ matches: coarsePointer })

      render(<HeroParticleBackground />)

      expect(screen.getByTestId('particle-canvas')).toHaveAttribute('data-particle-count', expected)
    },
  )

  it('does not require matchMedia support to select a particle count', () => {
    Object.defineProperty(window.navigator, 'hardwareConcurrency', { configurable: true, value: 12 })
    Object.defineProperty(window, 'devicePixelRatio', { configurable: true, value: 1 })
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1440 })
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 })
    window.matchMedia = undefined as unknown as typeof window.matchMedia

    render(<HeroParticleBackground />)

    expect(screen.getByTestId('particle-canvas')).toHaveAttribute('data-particle-count', '32000')
  })
})
