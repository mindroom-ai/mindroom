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
import { useDarkMode } from '@/hooks/useDarkMode'

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
    <div className="rounded-xl border border-orange-200 bg-orange-50 px-4 py-3 text-sm text-orange-900 dark:border-orange-900/40 dark:bg-orange-950/30 dark:text-orange-100">
      <p className="font-medium">{label} is not available yet.</p>
      <p className="mt-1 text-orange-800 dark:text-orange-200">
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
  const { isDarkMode } = useDarkMode()
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
              brand: '#f97316',
              brandAccent: '#ea580c',
              inputBackground: isDarkMode ? '#1f2937' : 'white',
              inputBorder: isDarkMode ? '#374151' : '#e5e7eb',
              inputBorderHover: isDarkMode ? '#4b5563' : '#d1d5db',
              inputBorderFocus: '#f97316',
              inputText: isDarkMode ? '#f3f4f6' : '#1f2937',
              inputPlaceholder: isDarkMode ? '#9ca3af' : '#6b7280',
            },
            radii: {
              borderRadiusButton: '0.75rem',
              buttonBorderRadius: '0.75rem',
              inputBorderRadius: '0.75rem',
            },
          },
        },
        className: {
          button: 'w-full px-4 py-3 font-semibold rounded-xl transition-all hover:shadow-lg hover:scale-[1.02]',
          input: 'w-full px-4 py-3 border rounded-xl focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent transition-all',
          label: 'block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2',
          anchor: 'text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300 font-medium transition-colors',
          message: 'text-red-600 dark:text-red-400 text-sm',
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
      <div className="rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700 dark:border-gray-700 dark:bg-gray-900/40 dark:text-gray-300">
        Loading authentication...
      </div>
    )
  }

  if (runtimeConfig === null || !isSupabaseConfigured(runtimeConfig)) {
    return <UnavailableAuth view={view} />
  }

  return <ConfiguredAuthWrapper view={view} redirectTo={redirectTo} runtimeConfig={runtimeConfig} />
}
