'use client'

import { AuthWrapper } from '@/components/auth/auth-wrapper'
import Link from 'next/link'

export default function SignupPage() {

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-2xl shadow-xl p-8">
        <Link href="/" className="flex items-center justify-center mb-8">
          <span className="text-5xl">🧠</span>
        </Link>
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold">Create Your MindRoom</h1>
          <p className="text-gray-600 mt-2">Start your 14-day free trial</p>
        </div>

        <AuthWrapper view="sign_up" />

        <div className="mt-6 text-center">
          <p className="text-gray-600">
            Already have an account?{' '}
            <Link href="/auth/login" className="text-orange-600 hover:text-orange-700 font-medium">
              Sign in
            </Link>
          </p>
        </div>

        <div className="mt-6 pt-6 border-t">
          <p className="text-xs text-gray-500 text-center">
            By signing up, you agree to our{' '}
            <Link href="/terms" className="text-orange-600 hover:text-orange-700">
              Terms of Service
            </Link>{' '}
            and{' '}
            <Link href="/privacy" className="text-orange-600 hover:text-orange-700">
              Privacy Policy
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
