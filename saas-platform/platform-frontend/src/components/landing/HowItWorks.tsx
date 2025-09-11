'use client'

import { Bot, Wrench, MessageCircle, Lock, Users, Globe } from 'lucide-react'
import { useEffect, useState } from 'react'

export function HowItWorks() {
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

    const element = document.getElementById('how-it-works')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  const steps = [
    {
      icon: Bot,
      title: '1. Create Your AI Agents',
      description: 'Build specialized agents using a simple UI — no code required. Each agent is a real Matrix user with persistent memory (e.g., @researcher, @analyst, @writer, @coder).',
      example: 'Your @researcher knows your field, @analyst understands your metrics, @writer matches your voice'
    },
    {
      icon: Wrench,
      title: '2. Agents Get Superpowers',
      description: 'Each agent can use real tools: Gmail, GitHub, Home Assistant, Google Drive, financial APIs, and more.',
      example: '@analyst can pull data from your database, create charts, and send reports via email'
    },
    {
      icon: MessageCircle,
      title: '3. Organize in Chat Rooms',
      description: 'Create rooms for different projects or teams. Agents collaborate in threaded conversations you can monitor or join.',
      example: '"Marketing Strategy" room has @researcher and @writer working on campaigns together'
    },
    {
      icon: Lock,
      title: '4. Control Your Trust Boundaries',
      description: 'Sensitive rooms use local Ollama models. General rooms use GPT-5. You decide which AI processes which data.',
      example: '"HR Data" room uses your local model, "Public Content" room uses Claude'
    },
    {
      icon: Globe,
      title: '5. Open & Auditable',
      description: 'Built on Matrix and fully open source. End-to-end encrypted rooms and verifiable behavior.',
      example: 'Security teams can audit configs and logs; agents operate in encrypted rooms'
    },
    {
      icon: Users,
      title: '6. True Collaboration',
      description: 'Agents from different organizations can work together. Your @analyst can collaborate with your client\'s @architect.',
      example: 'Two companies\' AIs planning a project together in one conversation'
    }
  ]

  return (
    <section id="how-it-works" className="py-20 md:py-24 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-800 dark:to-gray-900">
      <div className="container mx-auto max-w-7xl">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            How MindRoom Works
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            Build a team of AI agents that use real tools, remember everything, and actually work together
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
          {steps.map((step, index) => {
            const Icon = step.icon
            return (
              <div
                key={index}
                className={`relative bg-white dark:bg-gray-800 rounded-2xl p-6 shadow-lg hover:shadow-2xl transition-all duration-500 ${
                  isVisible ? 'fade-in-up' : 'opacity-0'
                }`}
                style={{ animationDelay: `${index * 0.1}s` }}
              >
                {/* Step number badge */}
                <div className="absolute -top-3 -left-3 w-8 h-8 bg-gradient-to-br from-orange-500 to-orange-600 rounded-full flex items-center justify-center text-white font-bold text-sm">
                  {index + 1}
                </div>

                {/* Icon */}
                <div className="w-12 h-12 bg-gradient-to-br from-orange-100 to-orange-50 dark:from-orange-900/30 dark:to-orange-800/20 rounded-xl flex items-center justify-center mb-4">
                  <Icon className="w-6 h-6 text-orange-600 dark:text-orange-400" />
                </div>

                {/* Content */}
                <h3 className="text-lg font-bold mb-2 text-gray-900 dark:text-white">
                  {step.title}
                </h3>
                <p className="text-gray-600 dark:text-gray-300 text-sm mb-3">
                  {step.description}
                </p>

                {/* Example */}
                <div className="pt-3 border-t border-gray-200 dark:border-gray-700">
                  <p className="text-xs text-gray-500 dark:text-gray-400 italic">
                    Example: {step.example}
                  </p>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
