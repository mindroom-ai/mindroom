'use client'

import { Auth } from '@supabase/auth-ui-react'
import { ThemeSupa } from '@supabase/auth-ui-shared'
import { createClient } from '@/lib/supabase/client'
import { useEffect, useState } from 'react'
import { useDarkMode } from '@/hooks/useDarkMode'

interface AuthWrapperProps {
  view?: 'sign_in' | 'sign_up'
  redirectTo?: string
}

export function AuthWrapper({ view = 'sign_in', redirectTo }: AuthWrapperProps) {
  const [origin, setOrigin] = useState('')
  const { isDarkMode } = useDarkMode()

  useEffect(() => {
    setOrigin(window.location.origin)
  }, [])

  const supabase = createClient()

  const computedRedirect = redirectTo && redirectTo.startsWith('http')
    ? redirectTo
    : `${origin}${redirectTo || '/auth/callback'}`

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
              borderRadiusButton: '0.5rem',
              buttonBorderRadius: '0.5rem',
              inputBorderRadius: '0.5rem',
            },
          },
        },
        className: {
          button: 'w-full px-4 py-2.5 font-medium rounded-lg transition-colors',
          input: 'w-full px-4 py-2.5 border rounded-lg focus:outline-none focus:ring-2 focus:ring-orange-500 focus:border-transparent',
          label: 'block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5',
          anchor: 'text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300',
          message: 'text-red-600 dark:text-red-400 text-sm',
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
