import { createBrowserClient } from '@supabase/ssr'
import { getBrowserRuntimeConfig } from '@/lib/runtime-config'
import type { Database } from './types'

export function createClient() {
  const { supabaseUrl, supabaseAnonKey } = getBrowserRuntimeConfig()

  return createBrowserClient<Database>(supabaseUrl, supabaseAnonKey)
}
