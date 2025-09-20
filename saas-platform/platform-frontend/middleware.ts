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

  return response
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
