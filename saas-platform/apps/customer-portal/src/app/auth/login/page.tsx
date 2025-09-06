'use client'

import { Auth } from '@supabase/auth-ui-react'
import { ThemeSupa } from '@supabase/auth-ui-shared'
import { createClient } from '@/lib/supabase/client'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'
import Link from 'next/link'

export default function LoginPage() {
  const supabase = createClient()
  const router = useRouter()

  useEffect(() => {
    // Check if already logged in
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      if (session) {
        router.push('/dashboard')
      }
    })

    return () => subscription.unsubscribe()
  }, [router, supabase.auth])

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-2xl shadow-xl p-8">
        <Link href="/" className="flex items-center justify-center mb-8">
          <span className="text-5xl">🧠</span>
        </Link>
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold">Welcome Back</h1>
          <p className="text-gray-600 mt-2">Sign in to access your MindRoom</p>
        </div>

        <Auth
          supabaseClient={supabase}
          appearance={{
            theme: ThemeSupa,
            variables: {
              default: {
                colors: {
                  brand: '#f97316',
                  brandAccent: '#ea580c',
                },
              },
            },
          }}
          providers={['google', 'github']}
          redirectTo={`${typeof window !== 'undefined' ? window.location.origin : ''}/auth/callback`}
          showLinks={true}
          view="sign_in"
        />

        <div className="mt-6 text-center">
          <p className="text-gray-600">
            Don't have an account?{' '}
            <Link href="/auth/signup" className="text-orange-600 hover:text-orange-700 font-medium">
              Sign up
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
