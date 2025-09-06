import { createServerClientSupabase } from '@/lib/supabase/server'
import { NextResponse } from 'next/server'
import { NextRequest } from 'next/server'

export async function GET(request: NextRequest) {
  const requestUrl = new URL(request.url)
  const code = requestUrl.searchParams.get('code')

  if (code) {
    const supabase = await createServerClientSupabase()
    await supabase.auth.exchangeCodeForSession(code)
  }

  // URL to redirect to after sign in process completes
  // Use the public app URL from environment or construct from headers
  const publicUrl = process.env.NEXT_PUBLIC_APP_URL ||
    `https://${request.headers.get('host')}` ||
    request.url

  return NextResponse.redirect(new URL('/dashboard', publicUrl))
}
