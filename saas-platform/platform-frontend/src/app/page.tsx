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
              <MindRoomLogo className="text-orange-500 group-hover:scale-110 transition-transform duration-300" size={32} />
              <span className="text-xl sm:text-2xl font-bold bg-gradient-to-r from-orange-500 to-orange-600 bg-clip-text text-transparent">
                MindRoom
              </span>
            </div>

            <div className="hidden lg:flex items-center gap-8">
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
          </div>
        </div>
      </footer>
    </main>
  )
}
