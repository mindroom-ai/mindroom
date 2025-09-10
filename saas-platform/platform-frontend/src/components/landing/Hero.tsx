import Link from 'next/link'
import { ArrowRight, Bot, MessageSquare, Shield } from 'lucide-react'

export function Hero() {
  return (
    <section className="pt-32 pb-20 px-6 dark:bg-gray-900">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center">
          <div className="inline-flex items-center px-4 py-2 bg-orange-100 dark:bg-orange-900/20 rounded-full mb-6">
            <span className="text-orange-700 dark:text-orange-400 text-sm font-medium">
              🚀 Deploy AI agents in minutes, not months
            </span>
          </div>

          <h1 className="text-5xl md:text-6xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-gray-100 dark:to-gray-400 bg-clip-text text-transparent">
            Your AI Agents,
            <br />
            Everywhere You Work
          </h1>

          <p className="text-xl text-gray-600 dark:text-gray-300 mb-8 max-w-2xl mx-auto">
            Deploy intelligent AI assistants across all your communication platforms.
            From Slack to Email to your custom app - MindRoom brings AI to where you already work.
          </p>

          <div className="flex flex-col sm:flex-row gap-4 justify-center mb-12">
            <Link
              href="/auth/signup"
              className="inline-flex items-center px-6 py-3 bg-orange-500 text-white font-medium rounded-lg hover:bg-orange-600 transition-colors"
            >
              Start Free Trial
              <ArrowRight className="ml-2 w-4 h-4" />
            </Link>
            <Link
              href="#features"
              className="inline-flex items-center px-6 py-3 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 font-medium rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
            >
              Learn More
            </Link>
          </div>

          {/* Feature Pills */}
          <div className="flex flex-wrap gap-4 justify-center">
            <div className="flex items-center gap-2 px-4 py-2 bg-white dark:bg-gray-800 rounded-full shadow-sm">
              <Bot className="w-4 h-4 text-orange-500" />
              <span className="text-sm text-gray-700 dark:text-gray-300">Multiple AI Models</span>
            </div>
            <div className="flex items-center gap-2 px-4 py-2 bg-white dark:bg-gray-800 rounded-full shadow-sm">
              <MessageSquare className="w-4 h-4 text-orange-500" />
              <span className="text-sm text-gray-700 dark:text-gray-300">10+ Integrations</span>
            </div>
            <div className="flex items-center gap-2 px-4 py-2 bg-white dark:bg-gray-800 rounded-full shadow-sm">
              <Shield className="w-4 h-4 text-orange-500" />
              <span className="text-sm text-gray-700 dark:text-gray-300">Enterprise Security</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
