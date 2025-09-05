import Link from 'next/link'
import { Hero } from '@/components/landing/Hero'
import { Features } from '@/components/landing/Features'
import { Pricing } from '@/components/landing/Pricing'

export default function LandingPage() {
  return (
    <main className="min-h-screen">
      {/* Navigation */}
      <nav className="fixed top-0 w-full bg-white/80 backdrop-blur-xl border-b z-50">
        <div className="container mx-auto px-6 py-4 flex justify-between items-center">
          <div className="flex items-center gap-2">
            <span className="text-3xl">🧠</span>
            <span className="text-2xl font-bold">MindRoom</span>
          </div>
          <div className="flex gap-4">
            <Link href="/auth/login" className="px-4 py-2 text-gray-600 hover:text-gray-900">
              Sign In
            </Link>
            <Link href="/auth/signup" className="px-4 py-2 bg-orange-500 text-white rounded-lg hover:bg-orange-600 transition-colors">
              Get Started
            </Link>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <Hero />

      {/* Features */}
      <Features />

      {/* Pricing */}
      <Pricing />

      {/* Footer */}
      <footer className="bg-gray-900 text-white py-12">
        <div className="container mx-auto px-6 text-center">
          <p>© 2024 MindRoom. Your AI, everywhere.</p>
        </div>
      </footer>
    </main>
  )
}
