import { render, screen } from '@testing-library/react'

jest.mock('@basnijholt/particular-drift/react', () => ({
  ParticularDriftCanvas: ({ className, imageUrl, options }: {
    className?: string
    imageUrl: string
    options: { particleColor: string }
  }) => (
    <canvas
      className={className}
      data-image-url={imageUrl}
      data-particle-color={options.particleColor}
      data-testid="particle-canvas"
    />
  ),
}), { virtual: true })

import { HeroParticleBackground } from '../HeroParticleBackground'

describe('HeroParticleBackground', () => {
  it('renders a scoped MindRoom logo particle canvas for the landing hero', () => {
    render(<HeroParticleBackground />)

    const background = screen.getByTestId('landing-particle-background')
    const canvas = screen.getByTestId('particle-canvas')

    expect(background).toHaveAttribute('aria-hidden', 'true')
    expect(background).toHaveClass('absolute')
    expect(background).toHaveClass('lg:block')
    expect(canvas).toHaveAttribute('data-image-url', '/res/branding/mindroom.svg')
    expect(canvas).toHaveAttribute('data-particle-color', '#dda290')
  })
})
