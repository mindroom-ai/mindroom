'use client'

import Link from 'next/link'
import { Hero } from '@/components/landing/Hero'
import { HowItWorks } from '@/components/landing/HowItWorks'
import { Features } from '@/components/landing/Features'
import { Pricing } from '@/components/landing/Pricing'
import { Testimonials } from '@/components/landing/Testimonials'
import { Stats } from '@/components/landing/Stats'
import { CTA } from '@/components/landing/CTA'
import { WhyItMatters } from '@/components/landing/WhyItMatters'
import { Collaboration } from '@/components/landing/Collaboration'
import { DarkModeToggle } from '@/components/DarkModeToggle'
import { MindRoomLogo } from '@/components/MindRoomLogo'
import { useState, useEffect } from 'react'
import { navLinks, footerLinks } from '@/lib/constants'
import {
  FileText, DollarSign, Layers,
  Info, BookOpen, Briefcase,
  Mail, HelpCircle, Activity
} from 'lucide-react'

export default function LandingPage() {
  const [scrolled, setScrolled] = useState(false)

  useEffect(() => {
    const handleScroll = () => {
      setScrolled(window.scrollY > 20)
    }
    window.addEventListener('scroll', handleScroll)
    return () => window.removeEventListener('scroll', handleScroll)
  }, [])

  // Icon mapping for footer links
  const footerIcons: Record<string, any> = {
    'Features': Layers,
    'Pricing': DollarSign,
    'Documentation': FileText,
    'About': Info,
    'Blog': BookOpen,
    'Careers': Briefcase,
    'Contact': Mail,
    'Help Center': HelpCircle,
    'Status': Activity,
  }

  return (
    <main className="min-h-screen overflow-x-hidden">
      {/* Modern Navigation with Glass Effect */}
      <nav className={`fixed top-0 w-full z-50 transition-all duration-300 ${
        scrolled
          ? 'glass-effect shadow-lg'
          : 'bg-transparent'
      }`}>
        <div className="container mx-auto px-4 sm:px-6 py-4">
          <div className="flex justify-between items-center">
            <div className="flex items-center gap-2 sm:gap-3 group">
              <MindRoomLogo className="text-orange-500 group-hover:scale-110 active:scale-100 transition-transform duration-300" size={32} />
              <span className="text-xl sm:text-2xl font-bold bg-gradient-to-r from-orange-500 to-orange-600 bg-clip-text text-transparent">
                MindRoom
              </span>
            </div>

            <div className="hidden xl:flex items-center gap-8">
              {navLinks.map((link) => (
                <Link
                  key={link.href}
                  href={link.href}
                  className="nav-link"
                >
                  {link.label}
                </Link>
              ))}
            </div>

            <div className="flex items-center gap-2 sm:gap-4">
              <DarkModeToggle />
              <Link
                href="/auth/login"
                className="hidden sm:flex items-center px-5 py-2 text-gray-600 dark:text-gray-300 hover:text-orange-500 dark:hover:text-orange-400 font-medium transition-colors whitespace-nowrap"
              >
                Sign In
              </Link>
              <Link
                href="/auth/signup"
                className="btn-primary shimmer text-sm sm:text-base px-4 sm:px-6 py-2 sm:py-2.5 whitespace-nowrap"
              >
                <span className="hidden sm:inline">Get Started Free</span>
                <span className="sm:hidden">Get Started</span>
              </Link>
            </div>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <Hero />

      {/* Option 2: Getting Started Banner */}
      <section className="py-8 bg-gradient-to-r from-blue-50 via-purple-50 to-blue-50 dark:from-blue-500/10 dark:via-purple-500/10 dark:to-blue-500/10">
        <div className="container mx-auto px-6 text-center">
          <div className="inline-flex items-center gap-2 px-4 py-2 bg-white/80 dark:bg-gray-800/80 backdrop-blur-sm rounded-full shadow-lg border border-gray-200 dark:border-gray-700 mb-4">
            <span className="text-2xl">🎉</span>
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              Fresh out of the oven!
            </span>
          </div>
          <h3 className="text-2xl font-bold mb-3 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Built in Public by an Open Source Veteran
          </h3>
          <p className="text-gray-600 dark:text-gray-400 max-w-2xl mx-auto mb-4">
            MindRoom just launched! Everything you see here is already implemented and working.
            We're actively adding more bridges and integrations. Join us on this journey!
          </p>
          <div className="flex items-center justify-center gap-4">
            <a
              href="https://github.com/mindroom-ai/mindroom"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 bg-gray-900 dark:bg-white text-white dark:text-gray-900 rounded-lg hover:bg-gray-800 dark:hover:bg-gray-100 transition-colors"
            >
              <svg className="w-5 h-5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z"/>
              </svg>
              Star on GitHub
            </a>
            <a
              href="https://twitter.com/basnijholt"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-gray-700 dark:text-gray-300"
            >
              Follow the journey
            </a>
          </div>
        </div>
      </section>

      {/* How It Works - Immediately explain what MindRoom is */}
      <HowItWorks />

      {/* Collaboration - Show the KEY differentiator: federation across organizations */}
      <Collaboration />

      {/* Stats Section - Build credibility with Matrix adoption */}
      <Stats />

      {/* Features - Comprehensive capability list */}
      <Features />

      {/* Why It Matters - Strategic importance of federated AI */}
      <WhyItMatters />

      {/* Testimonials - Social proof */}
      <Testimonials />

      {/* Pricing - Commercial details */}
      <Pricing />

      {/* CTA Section */}
      <CTA />

      {/* Modern Footer */}
      <footer className="relative bg-gradient-to-br from-gray-900 to-gray-800 text-white py-16 overflow-hidden">
        <div className="absolute inset-0 bg-gradient-to-t from-orange-500/5 to-transparent"></div>
        <div className="container mx-auto px-6 relative z-10">
          <div className="grid md:grid-cols-4 gap-8 mb-8">
            <div>
              <div className="flex items-center gap-2 mb-4">
                <MindRoomLogo className="text-white" size={32} />
                <span className="text-xl font-bold">MindRoom</span>
              </div>
              <p className="text-gray-400">
                Your AI agents, deployed everywhere you work.
              </p>
            </div>

            {Object.entries(footerLinks).map(([category, links]) => (
              <div key={category}>
                <h4 className="font-semibold mb-4 capitalize">{category}</h4>
                <ul className="space-y-2">
                  {links.map((link) => {
                    const Icon = footerIcons[link.label]
                    return (
                      <li key={link.href}>
                        <Link href={link.href} className="footer-link flex items-center gap-2 group">
                          {Icon && <Icon className="w-4 h-4 text-gray-500 group-hover:text-orange-400 transition-colors" />}
                          <span>{link.label}</span>
                        </Link>
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
          </div>

          <div className="border-t border-gray-700 pt-8 text-center">
            <p className="text-gray-400">© 2025 MindRoom. All rights reserved.</p>
            <p className="text-gray-400 mt-2">
              Made with ❤️ by{' '}
              <a
                href="https://github.com/basnijholt"
                target="_blank"
                rel="noopener noreferrer"
                className="text-orange-400 hover:text-orange-300 transition-colors"
              >
                an open source veteran
              </a>
            </p>
          </div>
        </div>
      </footer>
    </main>
  )
}
