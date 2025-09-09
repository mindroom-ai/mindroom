'use client'

import { AuthWrapper } from '@/components/auth/auth-wrapper'
import Link from 'next/link'

export default function SignupPage() {

  return (
    <div className="min-h-screen flex items-center justify-center px-4 bg-gray-50 dark:bg-gray-900">
      <div className="max-w-md w-full bg-white dark:bg-gray-800 rounded-2xl shadow-xl p-8">
        <Link href="/" className="flex items-center justify-center mb-8">
          <span className="text-5xl">🧠</span>
        </Link>
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold dark:text-white">Create Your MindRoom</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">Start your 14-day free trial</p>
        </div>

        <AuthWrapper view="sign_up" />

        <div className="mt-6 text-center">
          <p className="text-gray-600 dark:text-gray-400">
            Already have an account?{' '}
            <Link href="/auth/login" className="text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300 font-medium">
              Sign in
            </Link>
          </p>
        </div>

        <div className="mt-6 pt-6 border-t dark:border-gray-700">
          <p className="text-xs text-gray-500 dark:text-gray-500 text-center">
            By signing up, you agree to our{' '}
            <Link href="/terms" className="text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300">
              Terms of Service
            </Link>{' '}
            and{' '}
            <Link href="/privacy" className="text-orange-600 dark:text-orange-400 hover:text-orange-700 dark:hover:text-orange-300">
              Privacy Policy
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
