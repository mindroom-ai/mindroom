import { createServerClient } from '@supabase/ssr'
import { cookies } from 'next/headers'
import { getServerRuntimeConfig } from '@/lib/runtime-config'
import type { Database } from './types'

export async function createServerClientSupabase() {
  const cookieStore = await cookies()

  const { supabaseUrl, supabaseAnonKey } = getServerRuntimeConfig()

  return createServerClient<Database>(
    supabaseUrl,
    supabaseAnonKey,
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
