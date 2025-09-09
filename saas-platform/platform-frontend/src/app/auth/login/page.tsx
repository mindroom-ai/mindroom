import { AuthWrapper } from '@/components/auth/auth-wrapper'
import Link from 'next/link'

export default function LoginPage({ searchParams }: { searchParams: { redirect_to?: string } }) {
  const redirectTo = searchParams?.redirect_to || '/dashboard'

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-2xl shadow-xl p-8">
        <Link href="/" className="flex items-center justify-center mb-8">
          <span className="text-5xl">🧠</span>
        </Link>
        <div className="text-center mb-8">
          <h1 className="text-3xl font-bold">Welcome Back</h1>
          <p className="text-gray-600 mt-2">Sign in to access your MindRoom</p>
        </div>

        <AuthWrapper view="sign_in" redirectTo={`/auth/callback?next=${encodeURIComponent(redirectTo)}`} />

        <div className="mt-6 text-center">
          <p className="text-gray-600">
            Don't have an account?{' '}
            <Link href="/auth/signup" className="text-orange-600 hover:text-orange-700 font-medium">
              Sign up
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
