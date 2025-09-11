'use client'

import Link from 'next/link'
import { ArrowRight, Bot, MessageSquare, Shield, Sparkles, ChevronDown } from 'lucide-react'
import { useEffect, useState } from 'react'

export function Hero() {
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setMounted(true)
  }, [])

  return (
    <section className="relative min-h-screen flex items-center justify-center overflow-hidden">
      {/* Animated gradient background */}
      <div className="absolute inset-0 bg-gradient-to-br from-orange-50 via-white to-orange-50 dark:from-gray-900 dark:via-gray-800 dark:to-gray-900">
        <div className="absolute inset-0 bg-gradient-to-t from-transparent via-transparent to-white/50 dark:to-black/50"></div>
      </div>

      {/* Animated blob shapes */}
      <div className="blob-1 top-20 -left-48 hidden lg:block"></div>
      <div className="blob-2 bottom-20 -right-48 hidden lg:block"></div>

      {/* Grid pattern overlay */}
      <div className="absolute inset-0 bg-[url('data:image/svg+xml,%3Csvg%20width%3D%2260%22%20height%3D%2260%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Cdefs%3E%3Cpattern%20id%3D%22grid%22%20width%3D%2260%22%20height%3D%2260%22%20patternUnits%3D%22userSpaceOnUse%22%3E%3Cpath%20d%3D%22M%2060%200%20L%200%200%200%2060%22%20fill%3D%22none%22%20stroke%3D%22gray%22%20stroke-width%3D%220.5%22%20opacity%3D%220.1%22%2F%3E%3C%2Fpattern%3E%3C%2Fdefs%3E%3Crect%20width%3D%22100%25%22%20height%3D%22100%25%22%20fill%3D%22url(%23grid)%22%2F%3E%3C%2Fsvg%3E')] opacity-30 dark:opacity-10"></div>

      <div className="container mx-auto px-6 relative z-10 pt-32 pb-20">
        <div className="text-center max-w-5xl mx-auto">
          {/* Animated badge */}
          <div className={`inline-flex items-center gap-2 px-5 py-2 bg-gradient-to-r from-orange-100 to-orange-50 dark:from-orange-900/30 dark:to-orange-800/20 rounded-full mb-8 ${mounted ? 'fade-in-up' : 'opacity-0'}`}>
            <Sparkles className="w-4 h-4 text-orange-600 dark:text-orange-400" />
            <span className="text-orange-700 dark:text-orange-400 text-sm font-semibold">
              Create specialized AI agents with 80+ tools and persistent memory
            </span>
          </div>

          {/* Main heading with animated gradient */}
          <h1 className={`text-5xl md:text-7xl font-bold mb-6 ${mounted ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.1s' }}>
            <span className="bg-gradient-to-r from-gray-900 via-gray-700 to-gray-900 dark:from-white dark:via-gray-200 dark:to-white bg-clip-text text-transparent bg-300% animate-gradient">
              Build Your AI Team
            </span>
            <br />
            <span className="bg-gradient-to-r from-orange-600 via-orange-500 to-orange-600 bg-clip-text text-transparent bg-300% animate-gradient" style={{ animationDelay: '0.5s' }}>
              That Actually Collaborates
            </span>
          </h1>

          {/* Subheading */}
          <p className={`text-xl md:text-2xl text-gray-600 dark:text-gray-300 mb-10 max-w-3xl mx-auto leading-relaxed ${mounted ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.2s' }}>
            Decentralized AI agents with real chat accounts. Built entirely on open source giants like the Matrix protocol. Verifiable end-to-end encryption. Works everywhere via bridges.
          </p>

          {/* CTA Buttons */}
          <div className={`flex flex-col sm:flex-row gap-4 justify-center mb-16 ${mounted ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.3s' }}>
            <Link
              href="/auth/signup"
              className="group inline-flex items-center px-8 py-4 bg-gradient-to-r from-orange-500 to-orange-600 text-white font-semibold rounded-full hover:shadow-2xl hover:shadow-orange-500/25 transform hover:scale-105 active:scale-95 transition-all duration-300 shimmer"
            >
              Start Free Trial
              <ArrowRight className="ml-2 w-5 h-5 group-hover:translate-x-1 transition-transform" />
            </Link>
            <Link
              href="#features"
              className="inline-flex items-center px-8 py-4 bg-white/80 dark:bg-gray-800/80 backdrop-blur border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 font-semibold rounded-full hover:bg-white dark:hover:bg-gray-800 hover:border-orange-300 dark:hover:border-orange-700 hover:shadow-lg active:scale-95 transition-all duration-300"
            >
              Watch Demo
              <svg className="ml-2 w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                <path d="M10 12a2 2 0 100-4 2 2 0 000 4z" />
                <path fillRule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clipRule="evenodd" />
              </svg>
            </Link>
          </div>

          {/* Feature Pills with hover effects */}
          <div className={`flex flex-wrap gap-4 justify-center ${mounted ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.4s' }}>
            <div className="touch-card group flex items-center gap-2 px-5 py-3 bg-white/80 dark:bg-gray-800/80 backdrop-blur rounded-full shadow-md hover:shadow-xl hover:scale-105 transition-all duration-300 cursor-default">
              <Bot className="w-5 h-5 text-orange-500 group-hover:rotate-12 transition-transform" />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Truly Decentralized</span>
            </div>
            <div className="touch-card group flex items-center gap-2 px-5 py-3 bg-white/80 dark:bg-gray-800/80 backdrop-blur rounded-full shadow-md hover:shadow-xl hover:scale-105 transition-all duration-300 cursor-default">
              <MessageSquare className="w-5 h-5 text-orange-500 group-hover:rotate-12 transition-transform" />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Works Where You Work</span>
            </div>
            <div className="touch-card group flex items-center gap-2 px-5 py-3 bg-white/80 dark:bg-gray-800/80 backdrop-blur rounded-full shadow-md hover:shadow-xl hover:scale-105 transition-all duration-300 cursor-default">
              <Shield className="w-5 h-5 text-orange-500 group-hover:rotate-12 transition-transform" />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">End-to-End Encrypted</span>
            </div>
            <div className="touch-card group flex items-center gap-2 px-5 py-3 bg-white/80 dark:bg-gray-800/80 backdrop-blur rounded-full shadow-md hover:shadow-xl hover:scale-105 transition-all duration-300 cursor-default">
              <Sparkles className="w-5 h-5 text-orange-500 group-hover:rotate-12 transition-transform" />
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">100% Open Source</span>
            </div>
          </div>

          {/* Scroll indicator */}
          <div className="scroll-indicator">
            <ChevronDown className="w-6 h-6 text-gray-400 dark:text-gray-600" />
          </div>
        </div>
      </div>

      <style jsx>{`
        @keyframes gradient {
          0%, 100% {
            background-position: 0% 50%;
          }
          50% {
            background-position: 100% 50%;
          }
        }

        .animate-gradient {
          animation: gradient 3s ease infinite;
          background-size: 200% 200%;
        }

        .bg-300\% {
          background-size: 300% 300%;
        }
      `}</style>
    </section>
  )
}
