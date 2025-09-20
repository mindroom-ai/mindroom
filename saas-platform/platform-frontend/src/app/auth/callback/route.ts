import { getServerRuntimeConfig } from '@/lib/runtime-config'
import { createServerClientSupabase } from '@/lib/supabase/server'
import { logger } from '@/lib/logger'
import { NextResponse } from 'next/server'
import { NextRequest } from 'next/server'

export async function GET(request: NextRequest) {
  const requestUrl = new URL(request.url)
  const code = requestUrl.searchParams.get('code')
  const next = requestUrl.searchParams.get('next') || '/dashboard'

  const { apiUrl, platformDomain } = getServerRuntimeConfig()

  if (code) {
    const supabase = await createServerClientSupabase()
    const { error } = await supabase.auth.exchangeCodeForSession(code)

    if (!error) {
      // Get the session to make API call
      const { data: { session } } = await supabase.auth.getSession()

      if (session && next.startsWith('/admin')) {
        // Check if user is admin via API
        try {
          const response = await fetch(`${apiUrl}/my/account/admin-status`, {
            headers: {
              'Authorization': `Bearer ${session.access_token}`,
              'Content-Type': 'application/json',
            },
          })

          if (response.ok) {
            const data = await response.json()

            // If user is admin and was trying to go to admin, redirect there
            if (data.is_admin) {
              const publicUrl = platformDomain
                ? `https://app.${platformDomain}`
                : `https://${request.headers.get('host')}` || request.url
              // Use normal admin redirect
              return NextResponse.redirect(new URL(next, publicUrl))
            }
          }
        } catch (err) {
          logger.error('Error checking admin status:', err)
        }
      }
    }
  }

  // URL to redirect to after sign in process completes
  // Use the public app URL from environment or construct from headers
  const publicUrl = platformDomain
    ? `https://app.${platformDomain}`
    : `https://${request.headers.get('host')}` || request.url

  // Redirect to a client page that sets the SSO cookie before navigating to `next`
  const completeUrl = new URL('/auth/complete', publicUrl)
  completeUrl.searchParams.set('next', next)
  return NextResponse.redirect(completeUrl)
}
