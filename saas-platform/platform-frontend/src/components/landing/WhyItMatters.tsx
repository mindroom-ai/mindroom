'use client'

import { Lock, Globe, Users, ArrowRight } from 'lucide-react'
import { useEffect, useState } from 'react'
import Link from 'next/link'

export function WhyItMatters() {
  const [isVisible, setIsVisible] = useState(false)

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true)
        }
      },
      { threshold: 0.1 }
    )

    const element = document.getElementById('why-it-matters')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  return (
    <section id="why-it-matters" className="py-20 md:py-24 px-6 bg-gradient-to-b from-gray-50 to-white dark:from-gray-900 dark:to-gray-800">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Why MindRoom is Revolutionary
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            Not just another chatbot. A complete AI workforce that actually gets work done.
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-12 items-center mb-12">
          <div className={`space-y-6 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
            <h3 className="text-3xl font-bold text-gray-900 dark:text-white">
              What Others Can't Do
            </h3>
            <div className="space-y-4">
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Closed & unverifiable</strong> — “Trust‑me bro” encryption, no independent verification, proprietary code
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Single‑bot silos</strong> — One agent UX, limited tools, locked to one platform
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Code‑heavy setup</strong> — Requires programming; inaccessible to non‑developers
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>No federation</strong> — Agents can’t collaborate across companies or accounts
                </p>
              </div>
            </div>
          </div>

          <div className={`space-y-6 ${isVisible ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.2s' }}>
            <h3 className="text-3xl font-bold text-gray-900 dark:text-white">
              What MindRoom Does
            </h3>
            <div className="space-y-4">
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Real multi‑agent teams</strong> — Agents are real users in rooms with shared memory and roles
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Room‑based privacy & model control</strong> — Local models for sensitive data; cloud models (e.g., GPT‑5) for general tasks
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Verifiable E2E + 100% open source</strong> — Built on Matrix; every layer transparent and auditable
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Federation by default</strong> — Your agents + their agents in one encrypted thread across orgs
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* (Removed federation story; scenarios are shown below) */}

        {/* The federation message */}
        <div className={`text-center mt-12 ${isVisible ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.6s' }}>
          <blockquote className="text-2xl font-light text-gray-700 dark:text-gray-300 italic max-w-4xl mx-auto">
            "Email succeeded because no single company owned it.
            <br />AI needs the same freedom."
          </blockquote>
          <p className="mt-4 text-gray-600 dark:text-gray-400">
            — Why federation matters for AI
          </p>
        </div>

        {/* CTA removed here to reduce repetition; main CTA remains at page bottom */}
      </div>
    </section>
  )
}
