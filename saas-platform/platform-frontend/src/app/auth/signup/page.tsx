'use client'

import { AuthShell } from '@/components/auth/auth-shell'
import { AuthWrapper } from '@/components/auth/auth-wrapper'
import Link from 'next/link'

export default function SignupPage() {
  return (
    <AuthShell
      title="Create your MindRoom"
      subtitle="Start with a hosted workspace and bring agents into rooms."
      switchText="Already have an account?"
      switchHref="/auth/login"
      switchLabel="Sign in"
      footer={(
        <>
          By signing up, you agree to our{' '}
          <Link href="/terms" className="text-[#f4b47e] transition-colors hover:text-[#ffd6b0]">
            Terms of Service
          </Link>{' '}
          and{' '}
          <Link href="/privacy" className="text-[#f4b47e] transition-colors hover:text-[#ffd6b0]">
            Privacy Policy
          </Link>
          .
        </>
      )}
    >
      <AuthWrapper view="sign_up" />
    </AuthShell>
  )
}
