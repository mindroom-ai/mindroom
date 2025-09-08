import { createServerClient } from '@supabase/ssr'
import { cookies } from 'next/headers'
import type { Database } from './types'

export async function createServerClientSupabase() {
  const cookieStore = await cookies()

  // Use placeholder values during build time, real values will be injected at runtime
  const url = process.env.SUPABASE_URL || 'https://placeholder.supabase.co'
  const anonKey = process.env.SUPABASE_ANON_KEY || 'placeholder-key'

  return createServerClient<Database>(
    url,
    anonKey,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll()
        },
        setAll(cookiesToSet) {
          try {
            cookiesToSet.forEach(({ name, value, options }) => {
              cookieStore.set(name, value, options)
            })
          } catch (error) {
            // The `set` method was called from a Server Component.
            // This can be ignored if you have middleware refreshing
            // user sessions.
          }
        },
      },
    }
  )
}
