'use client'

import { AuthWrapper } from '@/components/auth/auth-wrapper'
import Link from 'next/link'
import { X } from 'lucide-react'
import { MindRoomLogo } from '@/components/MindRoomLogo'

export default function SignupPage() {

  return (
    <div className="min-h-screen flex items-center justify-center px-4 bg-gradient-to-br from-orange-50 via-white to-purple-50 dark:from-gray-900 dark:via-gray-800 dark:to-gray-900">
      {/* Background decoration */}
      <div className="absolute inset-0 overflow-hidden">
        <div className="absolute top-0 right-0 w-96 h-96 bg-gradient-to-r from-orange-200 to-pink-200 dark:from-orange-900/10 dark:to-pink-900/10 rounded-full filter blur-3xl opacity-20"></div>
        <div className="absolute bottom-0 left-0 w-96 h-96 bg-gradient-to-r from-blue-200 to-purple-200 dark:from-blue-900/10 dark:to-purple-900/10 rounded-full filter blur-3xl opacity-20"></div>
      </div>

      <div className="relative max-w-md w-full bg-white/95 dark:bg-gray-800/95 backdrop-blur-sm rounded-3xl shadow-2xl p-8 border border-gray-100 dark:border-gray-700">
        {/* Close button */}
        <Link
          href="/"
          className="absolute top-4 right-4 p-2 text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300 transition-colors rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700"
          aria-label="Return to home"
        >
          <X className="w-5 h-5" />
        </Link>

        {/* Logo */}
        <Link href="/" className="flex items-center justify-center gap-3 mb-8 group">
          <MindRoomLogo className="text-orange-500 group-hover:scale-110 transition-transform duration-300" size={40} />
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
