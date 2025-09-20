import { createServerClient } from '@supabase/ssr'
import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'
import { getServerRuntimeConfig } from '@/lib/runtime-config'

export async function middleware(request: NextRequest) {
  let response = NextResponse.next({
    request: {
      headers: request.headers,
    },
  })

  const { supabaseUrl, supabaseAnonKey, apiUrl } = getServerRuntimeConfig()

  const supabase = createServerClient(
    supabaseUrl,
    supabaseAnonKey,
    {
      cookies: {
        get(name: string) {
          return request.cookies.get(name)?.value
        },
        set(name: string, value: string, options: any) {
          request.cookies.set({
            name,
            value,
            ...options,
          })
          response = NextResponse.next({
            request: {
              headers: request.headers,
            },
          })
          response.cookies.set({
            name,
            value,
            ...options,
          })
        },
        remove(name: string, options: any) {
          request.cookies.set({
            name,
            value: '',
            ...options,
          })
          response = NextResponse.next({
            request: {
              headers: request.headers,
            },
          })
          response.cookies.set({
            name,
            value: '',
            ...options,
          })
        },
      },
    }
  )

  // Refresh session if needed
  const { data: { user } } = await supabase.auth.getUser()

  // ADMIN ROUTE PROTECTION
  if (request.nextUrl.pathname.startsWith('/admin')) {
    if (!user) {
      const loginUrl = new URL('/auth/login', request.url)
      loginUrl.searchParams.set('redirect_to', request.nextUrl.pathname)
      return NextResponse.redirect(loginUrl)
    }

    // Get session for API call
    const { data: { session } } = await supabase.auth.getSession()

    if (!session) {
      const loginUrl = new URL('/auth/login', request.url)
      loginUrl.searchParams.set('redirect_to', request.nextUrl.pathname)
      return NextResponse.redirect(loginUrl)
    }

    // Check admin status via API
    try {
      const apiResponse = await fetch(`${apiUrl}/my/account/admin-status`, {
        headers: {
          'Authorization': `Bearer ${session.access_token}`,
          'Content-Type': 'application/json',
        },
      })

      if (!apiResponse.ok) {
        // Admin check failed - redirect to dashboard
        return NextResponse.redirect(new URL('/dashboard', request.url))
      }

      const data = await apiResponse.json()

      if (!data.is_admin) {
        return NextResponse.redirect(new URL('/dashboard', request.url))
      }
    } catch (error) {
      // Admin check exception - redirect to dashboard
      return NextResponse.redirect(new URL('/dashboard', request.url))
    }
  }

  // Apply security headers dynamically so CSP reflects runtime configuration
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
    "media-src 'self'",
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
  response.headers.set('X-XSS-Protection', '1; mode=block')
  if (!isDev) {
    response.headers.set('Strict-Transport-Security', 'max-age=31536000; includeSubDomains; preload')
  }

  return response
}

function safeOrigin(input?: string) {
  if (!input) return undefined
  try {
    return new URL(input).origin
  } catch (error) {
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
     */
    '/((?!_next/static|_next/image|favicon.ico|auth/callback).*)',
  ],
}
