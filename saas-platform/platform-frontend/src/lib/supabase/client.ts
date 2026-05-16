import { createBrowserClient } from '@supabase/ssr'
import { getRuntimeConfig, isSupabaseConfigured, type RuntimeConfig } from '@/lib/runtime-config'
import type { Database } from './types'

export function createClient(config: RuntimeConfig = getRuntimeConfig()) {
  if (!isSupabaseConfigured(config)) {
    throw new Error('Supabase runtime configuration is missing')
  }

  const { supabaseUrl, supabaseAnonKey } = config

  return createBrowserClient<Database>(supabaseUrl, supabaseAnonKey)
}
