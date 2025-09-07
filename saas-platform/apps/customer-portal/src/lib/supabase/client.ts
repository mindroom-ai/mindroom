import { createBrowserClient } from '@supabase/ssr'
import type { Database } from './types'

export function createClient() {
  // Use environment variables (will be set at build time via Docker ARG)
  const url = process.env.SUPABASE_URL || 'https://placeholder.supabase.co'
  const anonKey = process.env.SUPABASE_ANON_KEY || 'placeholder-key'

  return createBrowserClient<Database>(url, anonKey)
}
