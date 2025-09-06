import { Bot, Zap, Shield, Globe, Users, BarChart } from 'lucide-react'

const features = [
  {
    icon: Bot,
    title: 'Multiple AI Models',
    description: 'Choose from GPT-4, Claude, Gemini, and more. Switch models on the fly based on your needs.',
  },
  {
    icon: Zap,
    title: 'Instant Deployment',
    description: 'Deploy your AI agents in minutes. No infrastructure setup or complex configuration required.',
  },
  {
    icon: Globe,
    title: 'Platform Agnostic',
    description: 'Works with Slack, Discord, Email, SMS, and any platform with an API.',
  },
  {
    icon: Shield,
    title: 'Enterprise Security',
    description: 'SOC 2 compliant with end-to-end encryption. Your data stays private and secure.',
  },
  {
    icon: Users,
    title: 'Team Collaboration',
    description: 'Share agents across your team. Set permissions and track usage across departments.',
  },
  {
    icon: BarChart,
    title: 'Analytics & Insights',
    description: 'Track agent performance, user interactions, and ROI with detailed analytics.',
  },
]

export function Features() {
  return (
    <section id="features" className="py-20 px-6 bg-white">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-12">
          <h2 className="text-4xl font-bold mb-4">
            Everything You Need to Deploy AI
          </h2>
          <p className="text-xl text-gray-600">
            Powerful features that make AI deployment simple and effective
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-8">
          {features.map((feature, index) => {
            const Icon = feature.icon
            return (
              <div key={index} className="p-6">
                <div className="w-12 h-12 bg-orange-100 rounded-lg flex items-center justify-center mb-4">
                  <Icon className="w-6 h-6 text-orange-600" />
                </div>
                <h3 className="text-xl font-semibold mb-2">{feature.title}</h3>
                <p className="text-gray-600">{feature.description}</p>
              </div>
            )
          })}
        </div>
      </div>
    </section>
  )
}
