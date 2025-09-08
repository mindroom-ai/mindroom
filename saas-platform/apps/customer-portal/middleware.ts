import { createServerClient } from '@supabase/ssr'
import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

export async function middleware(request: NextRequest) {
  let response = NextResponse.next({
    request: {
      headers: request.headers,
    },
  })

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
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
    console.log(`[MIDDLEWARE] Admin route access attempt: ${request.nextUrl.pathname}`)

    if (!user) {
      console.log('[MIDDLEWARE] No user found, redirecting to login')
      const loginUrl = new URL('/auth/login', request.url)
      loginUrl.searchParams.set('redirectTo', request.nextUrl.pathname)
      return NextResponse.redirect(loginUrl)
    }

    console.log(`[MIDDLEWARE] User found: ${user.id}, email: ${user.email}`)

    // Check admin status using service role key for reliable access
    const adminSupabase = createServerClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.SUPABASE_SERVICE_ROLE_KEY!,
      {
        cookies: {
          get(name: string) {
            return request.cookies.get(name)?.value
          },
          set() {}, // No-op for service role client
          remove() {}, // No-op for service role client
        },
      }
    )

    try {
      console.log(`[MIDDLEWARE] Checking admin status for user: ${user.id}`)
      const { data: account, error } = await adminSupabase
        .from('accounts')
        .select('is_admin')
        .eq('id', user.id)
        .single()

      if (error) {
        console.error('[MIDDLEWARE] Error checking admin status:', error)
        return NextResponse.redirect(new URL('/dashboard', request.url))
      }

      console.log(`[MIDDLEWARE] Account data:`, account)

      if (!account?.is_admin) {
        console.log('[MIDDLEWARE] User is not admin, redirecting to dashboard')
        return NextResponse.redirect(new URL('/dashboard', request.url))
      }

      console.log('[MIDDLEWARE] Admin access granted')
    } catch (error) {
      console.error('[MIDDLEWARE] Exception checking admin status:', error)
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
