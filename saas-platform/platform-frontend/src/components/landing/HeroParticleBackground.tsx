'use client'

import { useMemo } from 'react'
import type { ParticularDriftUserOptions } from '@basnijholt/particular-drift'
import { ParticularDriftCanvas } from '@basnijholt/particular-drift/react'

const DESKTOP_PARTICLE_COUNT = 32000
const BALANCED_PARTICLE_COUNT = 20000
const LOW_END_PARTICLE_COUNT = 9000
const MINDROOM_LOGO_SRC = '/res/branding/mindroom.svg'

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

export function HeroParticleBackground() {
  const particleCount = useMemo(resolveLandingParticleCount, [])
  const options = useMemo<ParticularDriftUserOptions>(
    () => ({
      imageFit: 'contain',
      interactive: false,
      cursorMode: 'repel',
      cursorRadius: 0.12,
      cursorStrength: 0.9,
      backgroundColor: '#0f0d2e',
      particleColor: '#dda290',
      particleCount,
      particleOpacity: 0.34,
      particleSize: 1,
      particleSpeed: 7,
      attractionStrength: 84,
      edgeThreshold: 0.32,
      flowFieldScale: 4,
      maxDevicePixelRatio: 1.15,
    }),
    [particleCount],
  )

  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-x-0 bottom-0 top-80 z-0 block overflow-hidden bg-gradient-to-b from-transparent via-[#0f0d2e]/55 to-[#0f0d2e] [mask-image:linear-gradient(to_bottom,transparent_0%,black_26%,black_100%)] [-webkit-mask-image:linear-gradient(to_bottom,transparent_0%,black_26%,black_100%)] lg:inset-y-0 lg:left-auto lg:w-[70%] lg:bg-gradient-to-l lg:from-[#0f0d2e] lg:via-[#0f0d2e]/95 lg:to-transparent lg:[mask-image:linear-gradient(to_left,black_62%,transparent_100%)] lg:[-webkit-mask-image:linear-gradient(to_left,black_62%,transparent_100%)] motion-reduce:hidden"
      data-testid="landing-particle-background"
    >
      <ParticularDriftCanvas
        className="relative h-full w-full opacity-[0.58] lg:opacity-80"
        imageUrl={MINDROOM_LOGO_SRC}
        options={options}
      />
      <div className="absolute inset-0 bg-gradient-to-b from-gray-50 via-gray-50/55 to-transparent dark:from-gray-950 dark:via-gray-950/55 lg:bg-gradient-to-r lg:via-transparent" />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_45%,rgba(221,162,144,0.16),transparent_48%)]" />
    </div>
  )
}
