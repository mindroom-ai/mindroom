import { AuthShell } from '@/components/auth/auth-shell'
import { AuthWrapper } from '@/components/auth/auth-wrapper'
import { getServerRuntimeConfig } from '@/lib/runtime-config'
import { headers } from 'next/headers'
import { sanitizePostAuthRedirect } from '@/lib/auth/redirect'

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ redirect_to?: string }> }) {
  // Use the request host only as a fallback for local or misconfigured deployments.
  const hdrs = await headers()
  const params = await searchParams
  const host = hdrs.get('host') || ''
  const { platformDomain } = getServerRuntimeConfig({ requireSupabase: false })
  const nextTarget = sanitizePostAuthRedirect(params?.redirect_to, platformDomain)
  const base = platformDomain
    ? `https://app.${platformDomain}`
    : (host ? `https://${host}` : '')
  const callback = `${base}/auth/callback?next=${encodeURIComponent(nextTarget)}`

  return (
    <AuthShell
      title="Welcome back"
      subtitle="Sign in to open your hosted MindRoom workspace."
      switchText="Don't have an account?"
      switchHref="/auth/signup"
      switchLabel="Sign up"
    >
      <AuthWrapper view="sign_in" redirectTo={callback} />
    </AuthShell>
  )
}
