'use client'

import { Auth } from '@supabase/auth-ui-react'
import { ThemeSupa } from '@supabase/auth-ui-shared'
import { createClient } from '@/lib/supabase/client'
import {
  getBrowserRuntimeConfig,
  isSupabaseConfigured,
  type RuntimeConfig,
} from '@/lib/runtime-config'
import { useEffect, useMemo, useState } from 'react'
import { useRouter } from 'next/navigation'

interface AuthWrapperProps {
  view?: 'sign_in' | 'sign_up'
  redirectTo?: string
}

function readRuntimeConfig(): RuntimeConfig | null {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    return getBrowserRuntimeConfig()
  } catch {
    return null
  }
}

function UnavailableAuth({ view }: Pick<AuthWrapperProps, 'view'>) {
  const label = view === 'sign_up' ? 'Account signup' : 'Sign in'

  return (
    <div className="rounded-lg border border-[#f4b47e]/25 bg-[#f4b47e]/10 px-4 py-3 text-sm text-[#ffe4cc]">
      <p className="font-medium">{label} is not available yet.</p>
      <p className="mt-1 text-[#ffd6b0]/86">
        The hosted account backend is not configured for this deployment.
      </p>
    </div>
  )
}

function ConfiguredAuthWrapper({
  view = 'sign_in',
  redirectTo,
  runtimeConfig,
}: AuthWrapperProps & { runtimeConfig: RuntimeConfig }) {
  const [origin, setOrigin] = useState('')
  const router = useRouter()

  useEffect(() => {
    setOrigin(window.location.origin)
  }, [])

  const supabase = useMemo(() => createClient(runtimeConfig), [runtimeConfig])

  const computedRedirect = useMemo(() => {
    if (redirectTo && redirectTo.startsWith('http')) return redirectTo
    const target = redirectTo || '/auth/callback'
    return origin ? `${origin}${target}` : target
  }, [redirectTo, origin])

  // For password sign-in flows, the Auth UI does not auto-redirect.
  // Redirect on SIGNED_IN so email/password follows the same callback chain as OAuth.
  useEffect(() => {
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === 'SIGNED_IN' && session) {
        router.replace(computedRedirect)
      }
    })
    return () => subscription.unsubscribe()
  }, [router, supabase.auth, computedRedirect])

  return (
    <Auth
      supabaseClient={supabase}
      view={view}
      appearance={{
        theme: ThemeSupa,
        variables: {
          default: {
            colors: {
              brand: '#f4b47e',
              brandAccent: '#ffd6b0',
              inputBackground: 'rgba(8, 10, 22, 0.46)',
              inputBorder: 'rgba(255, 255, 255, 0.18)',
              inputBorderHover: 'rgba(255, 255, 255, 0.3)',
              inputBorderFocus: '#f4b47e',
              inputText: '#f8f5ff',
              inputPlaceholder: 'rgba(248, 245, 255, 0.56)',
              messageText: '#fecaca',
            },
            radii: {
              borderRadiusButton: '0.625rem',
              buttonBorderRadius: '0.625rem',
              inputBorderRadius: '0.625rem',
            },
          },
        },
        className: {
          button: 'w-full rounded-lg border border-white/20 bg-white/12 px-4 py-3 font-semibold text-white shadow-none transition-colors hover:bg-white/18',
          input: 'w-full rounded-lg border border-white/16 bg-black/25 px-4 py-3 text-white placeholder:text-white/45 transition-colors focus:border-[#f4b47e] focus:outline-none focus:ring-2 focus:ring-[#f4b47e]/35',
          label: 'mb-2 block text-sm font-medium text-white/72',
          anchor: 'font-medium text-[#f4b47e] transition-colors hover:text-[#ffd6b0]',
          message: 'text-sm text-red-200',
          container: 'space-y-4',
        },
      }}
      redirectTo={computedRedirect}
      providers={['google', 'github']}
      showLinks={view === 'sign_in'}
      magicLink={false}
      onlyThirdPartyProviders={false}
    />
  )
}

/** Render hosted auth once browser runtime config resolves. */
export function AuthWrapper({ view = 'sign_in', redirectTo }: AuthWrapperProps) {
  const [runtimeConfig, setRuntimeConfig] = useState<RuntimeConfig | null | undefined>(undefined)

  useEffect(() => {
    setRuntimeConfig(readRuntimeConfig())
  }, [])

  if (runtimeConfig === undefined) {
    return (
      <div className="rounded-lg border border-white/16 bg-white/8 px-4 py-3 text-sm text-white/72">
        Loading authentication...
      </div>
    )
  }

  if (runtimeConfig === null || !isSupabaseConfigured(runtimeConfig)) {
    return <UnavailableAuth view={view} />
  }

  return <ConfiguredAuthWrapper view={view} redirectTo={redirectTo} runtimeConfig={runtimeConfig} />
}
