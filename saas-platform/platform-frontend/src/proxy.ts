import { createServerClient } from '@supabase/ssr'
import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { getServerRuntimeConfig, isSupabaseConfigured } from '@/lib/runtime-config'

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({
    request: {
      headers: request.headers,
    },
  })

  const runtimeConfig = getServerRuntimeConfig({ requireSupabase: false })
  const { supabaseUrl, supabaseAnonKey, apiUrl } = runtimeConfig

  // Without Supabase, as in local runs, there is no session to refresh.
  if (!isSupabaseConfigured(runtimeConfig)) {
    return withSecurityHeaders(response, supabaseUrl, apiUrl)
  }

  const supabase = createServerClient(
    supabaseUrl,
    supabaseAnonKey,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll()
        },
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => {
            request.cookies.set(name, value)
          })
          response = NextResponse.next({
            request: {
              headers: request.headers,
            },
          })
          cookiesToSet.forEach(({ name, value, options }) => {
            response.cookies.set(name, value, options)
          })
        },
      },
    }
  )

  // Refresh session if needed
  await supabase.auth.getUser()

  return withSecurityHeaders(response, supabaseUrl, apiUrl)
}

// Apply security headers dynamically so CSP reflects runtime configuration
function withSecurityHeaders(response: NextResponse, supabaseUrl: string, apiUrl: string) {
  const isDev = process.env.NODE_ENV !== 'production'
  const connectSrc = new Set(["'self'", 'https://api.stripe.com'])

  const supabaseOrigin = safeOrigin(supabaseUrl)
  if (supabaseOrigin) {
    connectSrc.add(supabaseOrigin)
    if (supabaseOrigin.startsWith('https://')) {
      connectSrc.add(`wss://${supabaseOrigin.replace('https://', '')}`)
    }
  }

  const apiOrigin = safeOrigin(apiUrl)
  if (apiOrigin) {
    connectSrc.add(apiOrigin)
  }

  if (isDev) {
    connectSrc.add('http://localhost:*')
    connectSrc.add('ws://localhost:*')
  }

  const cspDirectives = [
    "default-src 'self'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "img-src 'self' data: blob: https:",
    "font-src 'self' data:",
    isDev
      ? "script-src 'self' 'unsafe-inline' 'unsafe-eval'"
      : "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    `connect-src ${Array.from(connectSrc).join(' ')}`,
    "frame-src 'self' https://js.stripe.com https://hooks.stripe.com",
    "form-action 'self'",
    // Matches next.config: the landing page's product film is a GitHub attachment.
    "media-src 'self' https://github.com https://github-production-user-asset-6210df.s3.amazonaws.com",
    "worker-src 'self' blob:",
    isDev ? '' : 'upgrade-insecure-requests',
    'report-uri /api/csp-report',
  ].filter(Boolean).join('; ')

  response.headers.delete('Content-Security-Policy')
  response.headers.delete('Content-Security-Policy-Report-Only')
  response.headers.set(
    isDev ? 'Content-Security-Policy-Report-Only' : 'Content-Security-Policy',
    cspDirectives,
  )
  response.headers.set('Referrer-Policy', 'strict-origin-when-cross-origin')
  response.headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=(), payment=(self)')
  response.headers.set('X-Content-Type-Options', 'nosniff')
  response.headers.set('X-Frame-Options', 'DENY')
  if (!isDev) {
    response.headers.set('Strict-Transport-Security', 'max-age=31536000; includeSubDomains; preload')
  }

  return response
}

function safeOrigin(input?: string) {
  if (!input) return undefined
  try {
    return new URL(input).origin
  } catch {
    return undefined
  }
}

export const config = {
  matcher: [
    /*
     * Match all request paths except for the ones starting with:
     * - _next/static (static files)
     * - _next/image (image optimization files)
     * - favicon.ico (favicon file)
     * - api routes that don't need auth
     * - static images, so loading them does not ask Supabase for the user
     */
    '/((?!_next/static|_next/image|favicon.ico|auth/callback|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
}
