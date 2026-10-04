'use client'

import { useMemo, type CSSProperties } from 'react'
import type { ParticularDriftUserOptions } from '@basnijholt/particular-drift'
import { ParticularDriftCanvas } from '@basnijholt/particular-drift/react'
import { useDarkMode } from '@/hooks/useDarkMode'

const DESKTOP_PARTICLE_COUNT = 32000
const BALANCED_PARTICLE_COUNT = 20000
const LOW_END_PARTICLE_COUNT = 9000
const MINDROOM_LOGO_SRC = '/res/branding/mindroom.svg'
type ParticleBackgroundVariant = 'hero' | 'auth'

// Same palettes as the MindRoom Chat particle background: light mode inverts the dark one.
const PARTICLE_THEMES = {
  dark: { backdrop: '#0f0d2e', particles: '#dda290', glow: 'rgba(221,162,144,0.16)' },
  light: { backdrop: '#f6e8d6', particles: '#5636a3', glow: 'rgba(86,54,163,0.16)' },
}

type HeroParticleBackgroundProps = {
  variant?: ParticleBackgroundVariant
  className?: string
}

function resolveLandingParticleCount() {
  if (typeof window === 'undefined') {
    return BALANCED_PARTICLE_COUNT
  }

  const coarsePointer = window.matchMedia?.('(hover: none), (pointer: coarse)')?.matches ?? false
  const hardwareConcurrency = window.navigator.hardwareConcurrency ?? 4
  const devicePixelRatio = window.devicePixelRatio || 1
  const effectivePixelArea = window.innerWidth * window.innerHeight * devicePixelRatio ** 2

  if (coarsePointer || hardwareConcurrency <= 4) {
    return LOW_END_PARTICLE_COUNT
  }
  if (hardwareConcurrency <= 8 || devicePixelRatio > 1.5 || effectivePixelArea > 4_000_000) {
    return BALANCED_PARTICLE_COUNT
  }
  return DESKTOP_PARTICLE_COUNT
}

function variantClassName(variant: ParticleBackgroundVariant) {
  if (variant === 'auth') {
    return 'pointer-events-none fixed inset-0 z-0 block overflow-hidden bg-[#0f0d2e] motion-reduce:hidden'
  }

  return 'pointer-events-none absolute inset-x-0 bottom-0 top-80 z-0 block overflow-hidden bg-gradient-to-b from-transparent via-(--particle-backdrop)/55 to-(--particle-backdrop) [mask-image:linear-gradient(to_bottom,transparent_0%,black_26%,black_100%)] [-webkit-mask-image:linear-gradient(to_bottom,transparent_0%,black_26%,black_100%)] lg:inset-y-0 lg:left-auto lg:w-[70%] lg:bg-gradient-to-l lg:from-(--particle-backdrop) lg:via-(--particle-backdrop)/95 lg:to-transparent lg:[mask-image:linear-gradient(to_left,black_62%,transparent_100%)] lg:[-webkit-mask-image:linear-gradient(to_left,black_62%,transparent_100%)] motion-reduce:hidden'
}

function canvasClassName(variant: ParticleBackgroundVariant, isDarkMode: boolean) {
  if (variant === 'auth') {
    return 'relative h-full w-full opacity-90'
  }

  // Dark particles on a light page read as dust, so the light field stays faint.
  return isDarkMode ? 'relative h-full w-full opacity-[0.58] lg:opacity-80' : 'relative h-full w-full opacity-40 lg:opacity-55'
}

export function HeroParticleBackground({
  variant = 'hero',
  className = '',
}: HeroParticleBackgroundProps) {
  const particleCount = useMemo(resolveLandingParticleCount, [])
  const { isDarkMode } = useDarkMode()
  // The auth screen is dark in both modes.
  const theme = variant === 'auth' || isDarkMode ? PARTICLE_THEMES.dark : PARTICLE_THEMES.light
  const options = useMemo<ParticularDriftUserOptions>(
    () => ({
      imageFit: 'contain',
      interactive: variant === 'auth',
      cursorMode: 'repel',
      cursorRadius: variant === 'auth' ? 0.14 : 0.12,
      cursorStrength: variant === 'auth' ? 1.1 : 0.9,
      backgroundColor: theme.backdrop,
      particleColor: theme.particles,
      particleCount,
      particleOpacity: variant === 'auth' ? 0.46 : 0.34,
      particleSize: variant === 'auth' ? 1.1 : 1,
      particleSpeed: variant === 'auth' ? 9 : 7,
      attractionStrength: variant === 'auth' ? 96 : 84,
      edgeThreshold: 0.32,
      flowFieldScale: 4,
      maxDevicePixelRatio: 1.15,
    }),
    [particleCount, theme, variant],
  )

  return (
    <div
      aria-hidden="true"
      className={`${variantClassName(variant)} ${className}`}
      data-variant={variant}
      data-testid="landing-particle-background"
      style={{ '--particle-backdrop': theme.backdrop } as CSSProperties}
    >
      <ParticularDriftCanvas
        className={canvasClassName(variant, isDarkMode)}
        imageUrl={MINDROOM_LOGO_SRC}
        options={options}
      />
      {variant === 'auth' ? (
        <>
          <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_45%,rgba(221,162,144,0.18),rgba(15,13,46,0.12)_30%,rgba(15,13,46,1)_72%)]" />
          <div className="absolute inset-0 bg-[#0f0d2e]/20" />
        </>
      ) : (
        <>
          <div className="absolute inset-0 bg-gradient-to-b from-gray-50 via-gray-50/55 to-transparent dark:from-gray-950 dark:via-gray-950/55 lg:bg-gradient-to-r lg:via-transparent" />
          <div
            className="absolute inset-0"
            style={{ background: `radial-gradient(circle at 70% 45%, ${theme.glow}, transparent 48%)` }}
          />
        </>
      )}
    </div>
  )
}
