import { createBrowserClient } from '@supabase/ssr'
import { getRuntimeConfig } from '@/lib/runtime-config'
import type { Database } from './types'

export function createClient() {
  const { supabaseUrl, supabaseAnonKey } = getRuntimeConfig()

  return createBrowserClient<Database>(supabaseUrl, supabaseAnonKey)
}
