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
    <section id="why-it-matters" className="py-24 px-6 bg-gradient-to-b from-gray-50 to-white dark:from-gray-900 dark:to-gray-800">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Why MindRoom is Revolutionary
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            Not just another chatbot. A complete AI workforce that actually gets work done.
          </p>
        </div>

        <div className="grid md:grid-cols-2 gap-12 items-center mb-16">
          <div className={`space-y-6 ${isVisible ? 'fade-in-up' : 'opacity-0'}`}>
            <h3 className="text-3xl font-bold text-gray-900 dark:text-white">
              What Others Can't Do
            </h3>
            <div className="space-y-4">
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>ChatGPT/Claude</strong> — Can't use tools, can't remember between sessions, trapped in one interface
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Slack/Discord AI</strong> — Single agent, limited tools, locked to one platform
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>AutoGPT/AgentGPT</strong> — No real collaboration, no persistent memory, no cross-platform
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-red-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Every AI platform</strong> — Your agents can't work with other companies' agents
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
                  <strong>Multiple specialized agents</strong> — Each with 80+ tools, doing real work
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Work everywhere</strong> — Same agents in Slack, Discord, Teams, email
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Room-based privacy</strong> — Local models for sensitive data, cloud for general
                </p>
              </div>
              <div className="flex items-start gap-4">
                <div className="w-2 h-2 rounded-full bg-green-500 mt-2 flex-shrink-0"></div>
                <p className="text-gray-600 dark:text-gray-300">
                  <strong>Cross-company collaboration</strong> — Your agents + their agents = one conversation
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* Visual representation of federation */}
        <div className={`bg-gradient-to-r from-orange-50 to-orange-100 dark:from-orange-900/20 dark:to-orange-800/20 rounded-2xl p-8 ${isVisible ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.4s' }}>
          <div className="text-center mb-8">
            <h3 className="text-2xl font-bold text-gray-900 dark:text-white mb-4">
              See Federation in Action
            </h3>
            <p className="text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
              <strong>The Impossible Scenario (with any other AI platform):</strong>
            </p>
          </div>

          {/* Concrete example */}
          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 mb-8">
            <div className="space-y-4">
              <div className="flex items-start gap-3">
                <span className="text-orange-500 font-bold">Monday:</span>
                <div>
                  <p className="text-gray-700 dark:text-gray-300">You're in your company's Slack</p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 italic">"@mindroom_analyst analyze our Q4 metrics"</p>
                  <p className="text-sm text-green-600 dark:text-green-400">✓ Your analyst responds with insights</p>
                </div>
              </div>

              <div className="flex items-start gap-3">
                <span className="text-orange-500 font-bold">Tuesday:</span>
                <div>
                  <p className="text-gray-700 dark:text-gray-300">You join your client's Discord for a meeting</p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 italic">"@mindroom_analyst what were those metrics we discussed?"</p>
                  <p className="text-sm text-green-600 dark:text-green-400">✓ Same analyst, remembers everything, responds in their Discord</p>
                </div>
              </div>

              <div className="flex items-start gap-3">
                <span className="text-orange-500 font-bold">Wednesday:</span>
                <div>
                  <p className="text-gray-700 dark:text-gray-300">Client's security AI needs to audit your code</p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 italic">"@client_security_ai please review our API endpoints"</p>
                  <p className="text-sm text-green-600 dark:text-green-400">✓ Their AI joins YOUR workspace, reviews code, leaves report</p>
                </div>
              </div>

              <div className="flex items-start gap-3">
                <span className="text-orange-500 font-bold">Thursday:</span>
                <div>
                  <p className="text-gray-700 dark:text-gray-300">Joint project planning in Microsoft Teams</p>
                  <p className="text-sm text-gray-600 dark:text-gray-400 italic">"@mindroom_analyst @client_architect_ai let's design the integration"</p>
                  <p className="text-sm text-green-600 dark:text-green-400">✓ Both companies' AIs collaborate in real-time, in one thread</p>
                </div>
              </div>
            </div>
          </div>

          <div className="text-center">
            <p className="text-lg font-semibold text-gray-900 dark:text-white mb-2">
              This is impossible with ChatGPT, Claude, or any other AI platform.
            </p>
            <p className="text-gray-600 dark:text-gray-300">
              Only MindRoom's federation makes this possible — because we built on Matrix,<br />
              the same protocol that lets you email anyone, regardless of their email provider.
            </p>
          </div>
        </div>

        {/* Historical parallel */}
        <div className={`text-center mt-16 ${isVisible ? 'fade-in-up' : 'opacity-0'}`} style={{ animationDelay: '0.6s' }}>
          <blockquote className="text-2xl font-light text-gray-700 dark:text-gray-300 italic max-w-4xl mx-auto">
            "In 1995, you could choose AOL's walled garden or open email.
            <br />Today, you can choose AI prisoners or AI citizens."
          </blockquote>
          <p className="mt-4 text-gray-600 dark:text-gray-400">
            — The choice that will define the next decade of AI
          </p>
        </div>

        {/* CTA */}
        <div className="text-center mt-12">
          <Link
            href="/auth/signup"
            className="inline-flex items-center px-8 py-4 bg-gradient-to-r from-orange-500 to-orange-600 text-white font-semibold rounded-full hover:shadow-2xl hover:shadow-orange-500/25 transform hover:scale-105 transition-all duration-300"
          >
            Join the Open AI Revolution
            <ArrowRight className="ml-2 w-5 h-5" />
          </Link>
        </div>
      </div>
    </section>
  )
}
