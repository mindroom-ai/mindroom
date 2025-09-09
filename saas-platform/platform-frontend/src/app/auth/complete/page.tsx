'use client'

import { Suspense, useEffect } from 'react'
import { useSearchParams } from 'next/navigation'
import { setSsoCookie } from '@/lib/api'

export const dynamic = 'force-dynamic'

function CompleteInner() {
  const search = useSearchParams()
  const next = search.get('next') || '/dashboard'

  useEffect(() => {
    let canceled = false
    const go = async () => {
      try {
        await setSsoCookie()
      } catch {
        // ignore; user may still be able to proceed
      } finally {
        if (!canceled) {
          window.location.href = next
        }
      }
    }
    void go()
    return () => {
      canceled = true
    }
  }, [next])

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
