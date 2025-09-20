import { DEFAULT_API_URL, resolveApiUrl as resolveApiUrlFromEnv } from '../../runtime-config-shared.js'

export type RuntimeConfig = {
  apiUrl: string
  supabaseUrl: string
  supabaseAnonKey: string
  platformDomain: string
}

export { DEFAULT_API_URL }

export function resolveApiUrl(): string {
  return resolveApiUrlFromEnv(process.env)
}

function safeJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, '\\u003c')
}

export function getServerRuntimeConfig(): RuntimeConfig {
  const supabaseUrl = process.env.SUPABASE_URL
  const supabaseAnonKey = process.env.SUPABASE_ANON_KEY

  if (!supabaseUrl) {
    throw new Error('SUPABASE_URL must be provided at runtime')
  }

  if (!supabaseAnonKey) {
    throw new Error('SUPABASE_ANON_KEY must be provided at runtime')
  }
  const platformDomain = process.env.PLATFORM_DOMAIN || ''

  const apiUrl = resolveApiUrl()

  return {
    apiUrl,
    supabaseUrl,
    supabaseAnonKey,
    platformDomain,
  }
}

declare global {
  interface Window {
    __MINDROOM_CONFIG__?: RuntimeConfig
  }
}

export function getBrowserRuntimeConfig(): RuntimeConfig {
  if (typeof window === 'undefined') {
    throw new Error('getBrowserRuntimeConfig must be called in the browser')
  }

  const config = window.__MINDROOM_CONFIG__
  if (!config) {
    throw new Error('MindRoom runtime configuration not found in browser environment')
  }

  return config
}

export function getRuntimeConfig(): RuntimeConfig {
  if (typeof window !== 'undefined') {
    return getBrowserRuntimeConfig()
  }

  return getServerRuntimeConfig()
}

export function serializeRuntimeConfig(config: RuntimeConfig): string {
  return safeJson(config)
}
