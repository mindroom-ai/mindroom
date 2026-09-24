'use client'

import { Suspense, useEffect } from 'react'
import { useSearchParams } from 'next/navigation'
import { listInstances, setSsoCookie } from '@/lib/api'
import {
  DEFAULT_POST_AUTH_REDIRECT,
  isInstanceRedirect,
  isPlatformRedirect,
  sanitizePostAuthRedirect,
} from '@/lib/auth/redirect'
import { navigateTo } from '@/lib/navigation'
import { getRuntimeConfig } from '@/lib/runtime-config'

export const dynamic = 'force-dynamic'

/** Return whether the SSO cookie may follow the user to `next`: a platform host or their own instance. */
async function isApprovedRedirect(next: string, platformDomain: string): Promise<boolean> {
  if (isPlatformRedirect(next, platformDomain)) {
    return true
  }
  try {
    const { instances } = await listInstances()
    return isInstanceRedirect(next, instances.map((instance) => instance.frontend_url))
  } catch {
    return false
  }
}

function CompleteInner() {
  const search = useSearchParams()
  const { platformDomain } = getRuntimeConfig()
  const next = sanitizePostAuthRedirect(search.get('next'), platformDomain)

  useEffect(() => {
    let canceled = false
    const go = async () => {
      const approved = await isApprovedRedirect(next, platformDomain)
      if (approved) {
        try {
          await setSsoCookie()
        } catch {
          // ignore; user may still be able to proceed
        }
      }
      if (!canceled) {
        navigateTo(approved ? next : DEFAULT_POST_AUTH_REDIRECT)
      }
    }
    void go()
    return () => {
      canceled = true
    }
  }, [next, platformDomain])

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="text-center text-gray-600">Completing sign in...</div>
    </div>
  )
}

export default function AuthCompletePage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center text-gray-600">Completing sign in...</div>
      </div>
    }>
      <CompleteInner />
    </Suspense>
  )
}
