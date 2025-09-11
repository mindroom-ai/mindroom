'use client'

import { useEffect, useState } from 'react'
import { createClient } from '@/lib/supabase/client'
import { User } from '@supabase/supabase-js'
import { useRouter } from 'next/navigation'
import { clearSsoCookie } from '@/lib/api'

// Development-only mock user - only active when NEXT_PUBLIC_DEV_AUTH=true
const DEV_USER: User | null =
  process.env.NODE_ENV === 'development' &&
  process.env.NEXT_PUBLIC_DEV_AUTH === 'true'
    ? {
        id: 'dev-user-123',
        email: 'dev@mindroom.local',
        app_metadata: {},
        user_metadata: {},
        aud: 'authenticated',
        created_at: new Date().toISOString(),
      } as User
    : null

export function useAuth() {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)
  const supabase = createClient()
  const router = useRouter()

  useEffect(() => {
    // Use dev user if in development mode with flag
    if (DEV_USER) {
      setUser(DEV_USER)
      setLoading(false)
      return
    }

    // Get initial user
    const getUser = async () => {
      const { data: { user } } = await supabase.auth.getUser()
      setUser(user)
      setLoading(false)
    }

    getUser()

    // Listen for auth changes
    const { data: { subscription } } = supabase.auth.onAuthStateChange((event, session) => {
      setUser(session?.user ?? null)

      if (event === 'SIGNED_OUT') {
        router.push('/')
      }
    })

    return () => subscription.unsubscribe()
  }, [supabase.auth, router])

  const signOut = async () => {
    // In dev mode, just refresh the page
    if (DEV_USER) {
      router.push('/')
      return
    }

    try {
      await clearSsoCookie()
    } catch {
      // non-fatal
    } finally {
      await supabase.auth.signOut()
    }
  }

  return {
    user,
    loading,
    signOut,
  }
}
