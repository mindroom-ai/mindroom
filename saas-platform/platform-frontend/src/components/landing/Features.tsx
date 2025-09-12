'use client'

import { Bot, Zap, Shield, Globe, Users, BarChart, Brain, Lock, Rocket } from 'lucide-react'
import { useEffect, useState } from 'react'

const features = [
  {
    icon: Bot,
    title: 'Agents Are Real Users',
    description: 'Agents have their own Matrix accounts and join rooms like teammates. Persistent memory and skills per agent.',
    gradient: 'from-blue-500 to-cyan-500',
  },
  {
    icon: Zap,
    title: '100+ Tools & Integrations',
    description: 'Gmail, GitHub, Home Assistant, Google Drive, databases, APIs and more. Your agents can actually DO things, not just talk.',
    gradient: 'from-yellow-500 to-orange-500',
  },
  {
    icon: Brain,
    title: 'Room-Based Organization',
    description: 'Different rooms for different projects. Private rooms use local models; work rooms use GPT-5. You control the boundaries.',
    gradient: 'from-green-500 to-emerald-500',
  },
  {
    icon: Users,
    title: 'True Agent Collaboration',
    description: 'Agents work together in threaded conversations. @researcher finds data, @analyst processes it, @writer creates the report.',
    gradient: 'from-indigo-500 to-purple-500',
  },
  {
    icon: Globe,
    title: 'Works Everywhere You Do',
    description: 'Through Matrix bridges, your agents work in Slack, Discord, Teams, WhatsApp — anywhere your team communicates.',
    gradient: 'from-purple-500 to-pink-500',
  },
  {
    icon: Shield,
    title: 'Federation & Security',
    description: 'Built on Matrix with end-to-end encryption. Verifiable by design and open source; agents collaborate across organizations without sacrificing privacy.',
    gradient: 'from-red-500 to-orange-500',
  },
]

const additionalFeatures = [
  {
    icon: Lock,
    title: 'Your Models, Your Control',
    description: 'Mix and match: GPT-5, Claude, Llama, Mistral. Route by sensitivity and cost.',
    gradient: 'from-teal-500 to-cyan-500',
  },
  {
    icon: Shield,
    title: 'Verifiable E2E Encryption',
    description: 'Not trust-me-bro security. Matrix provides audited, end-to-end encrypted rooms for agent collaboration and data exchange.',
    gradient: 'from-emerald-500 to-green-600',
  },
  {
    icon: Bot,
    title: 'No-Code Agent Builder',
    description: 'Configure agents via UI — personas, tools, and room permissions. No programming required.',
    gradient: 'from-sky-500 to-cyan-600',
  },
  {
    icon: BarChart,
    title: 'Self-Host or Cloud',
    description: 'Run on your infrastructure or use our service. Switch anytime. No lock-in.',
    gradient: 'from-gray-600 to-gray-800',
  },
  {
    icon: Rocket,
    title: 'Deploy in Minutes',
    description: 'Not months. Simple configuration, instant agent deployment, immediate value.',
    gradient: 'from-rose-500 to-pink-500',
  },
  {
    icon: Brain,
    title: 'Open Source & Auditable',
    description: 'MindRoom and the Matrix protocol are open source. Inspect, verify, and extend every part of the system.',
    gradient: 'from-indigo-600 to-purple-600',
  },
]

export function Features() {
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

    const element = document.getElementById('features')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  return (
    <section id="features" className="py-20 md:py-24 px-6 relative overflow-hidden">
      {/* Background gradient */}
      <div className="absolute inset-0 bg-gradient-to-b from-gray-50 to-white dark:from-gray-900 dark:to-gray-800"></div>

      {/* Decorative elements */}
      <div className="absolute top-0 left-0 w-96 h-96 bg-gradient-to-r from-orange-200 to-pink-200 dark:from-orange-900/20 dark:to-pink-900/20 rounded-full filter blur-3xl opacity-20"></div>
      <div className="absolute bottom-0 right-0 w-96 h-96 bg-gradient-to-r from-blue-200 to-purple-200 dark:from-blue-900/20 dark:to-purple-900/20 rounded-full filter blur-3xl opacity-20"></div>

      <div className="container mx-auto max-w-7xl relative z-10">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Not Just Another Chatbot
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            Real AI agents that use real tools, work in real places, and solve real problems
          </p>
        </div>

        {/* Main features grid */}
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6 mb-12">
          {features.map((feature, index) => {
            const Icon = feature.icon
            return (
              <div
                key={index}
                className={`touch-card group relative p-8 bg-white dark:bg-gray-800 rounded-2xl shadow-lg hover:shadow-2xl transition-all duration-500 hover:-translate-y-2 cursor-default ${
                  isVisible ? 'fade-in-up' : 'opacity-0'
                }`}
                style={{ animationDelay: `${index * 0.1}s` }}
              >
                {/* Gradient border on hover */}
                <div className="absolute inset-0 bg-gradient-to-r opacity-0 group-hover:opacity-100 transition-opacity duration-500 rounded-2xl -z-10 blur-xl"
                  style={{ background: `linear-gradient(135deg, var(--tw-gradient-stops))` }}
                ></div>

                {/* Icon with gradient background */}
                <div className={`w-14 h-14 bg-gradient-to-br ${feature.gradient} rounded-xl flex items-center justify-center mb-6 group-hover:scale-110 transition-transform duration-300`}>
                  <Icon className="w-7 h-7 text-white" />
                </div>

                <h3 className="text-xl font-bold mb-3 text-gray-900 dark:text-white">
                  {feature.title}
                </h3>
                <p className="text-gray-600 dark:text-gray-300 leading-relaxed">
                  {feature.description}
                </p>

                {/* Hover accent */}
                <div className={`absolute bottom-0 left-0 w-full h-1 bg-gradient-to-r ${feature.gradient} rounded-b-2xl transform scale-x-0 group-hover:scale-x-100 transition-transform duration-500`}></div>
              </div>
            )
          })}
        </div>

        {/* Additional features - Bento grid style */}
        <div className="grid md:grid-cols-3 gap-6">
          {additionalFeatures.map((feature, index) => {
            const Icon = feature.icon
            return (
              <div
                key={index}
                className={`touch-card group relative p-6 bg-gradient-to-br from-gray-50 to-white dark:from-gray-800 dark:to-gray-900 rounded-xl border border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-700 transition-all duration-300 ${
                  isVisible ? 'fade-in-up' : 'opacity-0'
                }`}
                style={{ animationDelay: `${(features.length + index) * 0.1}s` }}
              >
                <div className="flex items-start gap-4">
                  <div className={`w-10 h-10 bg-gradient-to-br ${feature.gradient} rounded-lg flex items-center justify-center flex-shrink-0 group-hover:scale-110 transition-transform duration-300`}>
                    <Icon className="w-5 h-5 text-white" />
                  </div>
                  <div>
                    <h4 className="font-semibold text-gray-900 dark:text-white mb-1">
                      {feature.title}
                    </h4>
                    <p className="text-sm text-gray-600 dark:text-gray-400">
                      {feature.description}
                    </p>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
