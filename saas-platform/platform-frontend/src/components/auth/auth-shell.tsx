'use client'

import Link from 'next/link'
import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { HeroParticleBackground } from '@/components/landing/HeroParticleBackground'
import { MindRoomLogo } from '@/components/MindRoomLogo'

type AuthShellProps = {
  title: string
  subtitle: string
  switchText: string
  switchHref: string
  switchLabel: string
  children: ReactNode
  footer?: ReactNode
}

export function AuthShell({
  title,
  subtitle,
  switchText,
  switchHref,
  switchLabel,
  children,
  footer,
}: AuthShellProps) {
  return (
    <main className="auth-glass-page relative min-h-screen overflow-hidden bg-[#0f0d2e] px-4 py-6 text-white sm:px-6">
      <HeroParticleBackground variant="auth" />
      <div className="relative z-10 flex min-h-[calc(100vh-3rem)] items-center justify-center">
        <section className="relative w-full max-w-[440px] rounded-2xl border border-white/20 bg-[#12102a]/35 p-6 shadow-[0_24px_90px_rgba(0,0,0,0.42)] backdrop-blur-md backdrop-saturate-150 sm:p-8">
          <div className="pointer-events-none absolute inset-0 rounded-2xl shadow-[inset_0_1px_0_rgba(255,255,255,0.16)]" />
          <Link
            href="/"
            className="absolute right-4 top-4 rounded-lg border border-white/10 bg-white/8 p-2 text-white/62 transition-colors hover:bg-white/14 hover:text-white"
            aria-label="Return to home"
          >
            <X className="h-5 w-5" />
          </Link>

          <Link href="/" className="mx-auto mb-7 flex w-fit items-center gap-3" aria-label="MindRoom home">
            <MindRoomLogo className="h-11 w-11" size={44} />
            <span className="text-xl font-semibold text-white">MindRoom</span>
          </Link>

          <div className="mb-7 text-center">
            <h1 className="text-3xl font-semibold text-white">{title}</h1>
            <p className="mt-2 text-sm leading-6 text-white/68">{subtitle}</p>
          </div>

          {children}

          <div className="mt-6 text-center text-sm text-white/64">
            {switchText}{' '}
            <Link href={switchHref} className="font-medium text-[#f4b47e] transition-colors hover:text-[#ffd6b0]">
              {switchLabel}
            </Link>
          </div>

          {footer && (
            <div className="mt-6 border-t border-white/12 pt-5 text-center text-xs leading-5 text-white/48">
              {footer}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
