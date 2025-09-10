'use client'

import { Bot, Zap, Shield, Globe, Users, BarChart, Brain, Lock, Rocket } from 'lucide-react'
import { useEffect, useState } from 'react'

const features = [
  {
    icon: Bot,
    title: 'Multiple AI Models',
    description: 'Choose from GPT-4, Claude 3, Gemini, and more. Switch models instantly based on your needs.',
    gradient: 'from-blue-500 to-cyan-500',
  },
  {
    icon: Zap,
    title: 'Instant Deployment',
    description: 'Deploy AI agents in under 2 minutes. Zero infrastructure setup or DevOps knowledge required.',
    gradient: 'from-yellow-500 to-orange-500',
  },
  {
    icon: Globe,
    title: 'Universal Integration',
    description: 'Native support for Slack, Discord, Teams, Email, and 15+ platforms out of the box.',
    gradient: 'from-green-500 to-emerald-500',
  },
  {
    icon: Shield,
    title: 'Enterprise Security',
    description: 'SOC 2 Type II certified with end-to-end encryption and GDPR compliance built-in.',
    gradient: 'from-purple-500 to-pink-500',
  },
  {
    icon: Users,
    title: 'Team Collaboration',
    description: 'Role-based access control, shared workspaces, and real-time collaboration features.',
    gradient: 'from-indigo-500 to-purple-500',
  },
  {
    icon: BarChart,
    title: 'Advanced Analytics',
    description: 'Track performance, costs, and ROI with AI-powered insights and custom dashboards.',
    gradient: 'from-red-500 to-orange-500',
  },
]

const additionalFeatures = [
  {
    icon: Brain,
    title: 'Smart Memory',
    description: 'Agents remember context across conversations and learn from interactions.',
    gradient: 'from-teal-500 to-cyan-500',
  },
  {
    icon: Lock,
    title: 'Private Deployment',
    description: 'Run agents in your own infrastructure with full data control.',
    gradient: 'from-gray-600 to-gray-800',
  },
  {
    icon: Rocket,
    title: 'Auto-Scaling',
    description: 'Automatically scale from 1 to 1000s of concurrent conversations.',
    gradient: 'from-rose-500 to-pink-500',
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
    <section id="features" className="py-24 px-6 relative overflow-hidden">
      {/* Background gradient */}
      <div className="absolute inset-0 bg-gradient-to-b from-gray-50 to-white dark:from-gray-900 dark:to-gray-800"></div>

      {/* Decorative elements */}
      <div className="absolute top-0 left-0 w-96 h-96 bg-gradient-to-r from-orange-200 to-pink-200 dark:from-orange-900/20 dark:to-pink-900/20 rounded-full filter blur-3xl opacity-20"></div>
      <div className="absolute bottom-0 right-0 w-96 h-96 bg-gradient-to-r from-blue-200 to-purple-200 dark:from-blue-900/20 dark:to-purple-900/20 rounded-full filter blur-3xl opacity-20"></div>

      <div className="container mx-auto max-w-7xl relative z-10">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Everything You Need to Deploy AI
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            Powerful features that make AI deployment simple, secure, and scalable for teams of any size
          </p>
        </div>

        {/* Main features grid */}
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6 mb-12">
          {features.map((feature, index) => {
            const Icon = feature.icon
            return (
              <div
                key={index}
                className={`group relative p-8 bg-white dark:bg-gray-800 rounded-2xl shadow-lg hover:shadow-2xl transition-all duration-500 hover:-translate-y-2 cursor-default ${
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
                className={`group relative p-6 bg-gradient-to-br from-gray-50 to-white dark:from-gray-800 dark:to-gray-900 rounded-xl border border-gray-200 dark:border-gray-700 hover:border-orange-300 dark:hover:border-orange-700 transition-all duration-300 ${
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
