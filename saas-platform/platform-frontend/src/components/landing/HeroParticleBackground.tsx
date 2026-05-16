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

  const coarsePointer = window.matchMedia?.('(hover: none), (pointer: coarse)').matches ?? false
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
      className="pointer-events-none absolute inset-y-0 right-0 z-0 hidden w-[70%] overflow-hidden bg-gradient-to-l from-[#0f0d2e] via-[#0f0d2e]/95 to-transparent [mask-image:linear-gradient(to_left,black_62%,transparent_100%)] [-webkit-mask-image:linear-gradient(to_left,black_62%,transparent_100%)] lg:block motion-reduce:hidden"
      data-testid="landing-particle-background"
    >
      <ParticularDriftCanvas
        className="relative h-full w-full opacity-80"
        imageUrl={MINDROOM_LOGO_SRC}
        options={options}
      />
      <div className="absolute inset-0 bg-gradient-to-r from-gray-50 via-transparent to-transparent dark:from-gray-950" />
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_70%_45%,rgba(221,162,144,0.16),transparent_48%)]" />
    </div>
  )
}
