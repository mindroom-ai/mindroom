import { createServerClientSupabase } from '@/lib/supabase/server'
import { NextResponse } from 'next/server'
import { NextRequest } from 'next/server'

export async function GET(request: NextRequest) {
  const requestUrl = new URL(request.url)
  const code = requestUrl.searchParams.get('code')
  const next = requestUrl.searchParams.get('next') || '/dashboard'

  if (code) {
    const supabase = await createServerClientSupabase()
    const { error } = await supabase.auth.exchangeCodeForSession(code)

    if (!error) {
      // Check if user is admin to determine redirect
      const { data: { user } } = await supabase.auth.getUser()

      if (user) {
        const { data: account } = await supabase
          .from('accounts')
          .select('is_admin')
          .eq('id', user.id)
          .single()

        // If user is admin and was trying to go to admin, redirect there
        if (account?.is_admin && next.startsWith('/admin')) {
          const publicUrl = process.env.APP_URL ||
            `https://${request.headers.get('host')}` ||
            request.url
          return NextResponse.redirect(new URL(next, publicUrl))
        }
      }
    }
  }

  // URL to redirect to after sign in process completes
  // Use the public app URL from environment or construct from headers
  const publicUrl = process.env.APP_URL ||
    `https://${request.headers.get('host')}` ||
    request.url

  return NextResponse.redirect(new URL(next, publicUrl))
}
