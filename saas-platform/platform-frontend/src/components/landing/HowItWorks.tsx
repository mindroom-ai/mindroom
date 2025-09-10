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
      description: 'Build specialized agents: @researcher, @analyst, @writer, @coder. Each agent has its own Matrix account and persistent memory.',
      example: 'Your @researcher knows your field, @analyst understands your metrics, @writer matches your voice'
    },
    {
      icon: Wrench,
      title: '2. Agents Get Superpowers',
      description: 'Each agent can access 80+ tools: Gmail, GitHub, Spotify, Home Assistant, Google Drive, financial APIs, and more.',
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
      description: 'Sensitive rooms use local Ollama models. General rooms use GPT-4. You decide which AI processes which data.',
      example: '"HR Data" room uses your local model, "Public Content" room uses Claude'
    },
    {
      icon: Globe,
      title: '5. Work Everywhere',
      description: 'Through Matrix bridges, your agents work in Slack, Discord, Teams, WhatsApp — anywhere your team communicates.',
      example: 'Same @analyst works in your Slack, client\'s Discord, and partner\'s Teams'
    },
    {
      icon: Users,
      title: '6. True Collaboration',
      description: 'Agents from different organizations can work together. Your @analyst can collaborate with your client\'s @architect.',
      example: 'Two companies\' AIs planning a project together in one conversation'
    }
  ]

  return (
    <section id="how-it-works" className="py-24 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-800 dark:to-gray-900">
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

        {/* Visual example */}
        <div className="mt-16 bg-gradient-to-r from-orange-50 to-orange-100 dark:from-orange-900/20 dark:to-orange-800/20 rounded-2xl p-8">
          <h3 className="text-2xl font-bold text-center mb-8 text-gray-900 dark:text-white">
            See It In Action
          </h3>

          <div className="bg-white dark:bg-gray-800 rounded-xl p-6 font-mono text-sm">
            <div className="space-y-4">
              <div>
                <span className="text-gray-500">You:</span>
                <span className="ml-2 text-gray-700 dark:text-gray-300">@researcher @analyst analyze our competitors and create a report</span>
              </div>

              <div className="pl-4 border-l-2 border-orange-300 dark:border-orange-700">
                <div className="text-orange-600 dark:text-orange-400">@researcher:</div>
                <div className="text-gray-600 dark:text-gray-400">I\'ll gather data on your top 5 competitors...</div>
                <div className="text-xs text-gray-500 mt-1">[Accessing: Web search, Industry databases, News APIs]</div>
              </div>

              <div className="pl-4 border-l-2 border-blue-300 dark:border-blue-700">
                <div className="text-blue-600 dark:text-blue-400">@analyst:</div>
                <div className="text-gray-600 dark:text-gray-400">I\'ll analyze market positioning and create visualizations...</div>
                <div className="text-xs text-gray-500 mt-1">[Accessing: Data analysis tools, Chart generators, Google Sheets]</div>
              </div>

              <div className="pl-4 border-l-2 border-green-300 dark:border-green-700">
                <div className="text-green-600 dark:text-green-400">Together:</div>
                <div className="text-gray-600 dark:text-gray-400">Report complete! Sent to your email and saved to Google Drive.</div>
                <div className="text-xs text-gray-500 mt-1">[Tools used: 12 | Data processed: 847 sources | Time: 3 minutes]</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
