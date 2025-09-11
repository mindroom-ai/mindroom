'use client'

import { Star, Quote } from 'lucide-react'
import { useEffect, useState } from 'react'
import { trustCompanies } from '@/lib/constants'

const testimonials = [
  {
    name: 'Dr. Stefan Mueller',
    role: 'CTO Healthcare Network',
    company: 'German Healthcare System',
    content: 'We needed self-hosted AI that could still collaborate with partners. MindRoom\'s federation is revolutionary - our agents can securely join external organizations while our data stays on-premise.',
    avatar: 'SM',
    rating: 5,
  },
  {
    name: 'Sarah Chen',
    role: 'Head of AI Strategy',
    company: 'Global Consulting Firm',
    content: 'Our AI consultants can finally join client workspaces directly. They maintain context across Slack, Discord, and Teams - all while preserving our IP security. Game-changing for professional services.',
    avatar: 'SC',
    rating: 5,
  },
  {
    name: 'Michael Rodriguez',
    role: 'Engineering Director',
    company: 'Enterprise SaaS Platform',
    content: 'Migrated from ChatGPT. Now our agents remember every conversation across all platforms. The fact that two companies\' AIs can collaborate in one thread? That\'s the future we\'ve been waiting for.',
    avatar: 'MR',
    rating: 5,
  },
]

export function Testimonials() {
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

    const element = document.getElementById('testimonials')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  return (
    <section id="testimonials" className="py-20 md:py-24 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-800 dark:to-gray-900">
      <div className="container mx-auto max-w-6xl">
        <div className="text-center mb-16">
          <h2 className="text-4xl md:text-5xl font-bold mb-6 bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent">
            Trusted by Organizations That Can't Compromise
          </h2>
          <p className="text-xl text-gray-600 dark:text-gray-300 max-w-3xl mx-auto">
            From government agencies to healthcare systems, organizations that demand security and sovereignty choose MindRoom
          </p>
        </div>

        <div className="grid md:grid-cols-3 gap-8">
          {testimonials.map((testimonial, index) => (
            <div
              key={index}
              className={`group relative bg-white dark:bg-gray-800 rounded-2xl p-8 shadow-lg hover:shadow-2xl transition-all duration-500 ${
                isVisible ? 'fade-in-up' : 'opacity-0'
              }`}
              style={{ animationDelay: `${index * 0.15}s` }}
            >
              {/* Quote icon */}
              <Quote className="absolute top-6 right-6 w-8 h-8 text-orange-200 dark:text-orange-900/30" />

              {/* Rating stars */}
              <div className="flex gap-1 mb-4">
                {[...Array(testimonial.rating)].map((_, i) => (
                  <Star key={i} className="w-5 h-5 fill-orange-400 text-orange-400" />
                ))}
              </div>

              {/* Content */}
              <p className="text-gray-700 dark:text-gray-300 mb-6 leading-relaxed">
                "{testimonial.content}"
              </p>

              {/* Author */}
              <div className="flex items-center gap-4">
                <div className="w-12 h-12 bg-gradient-to-br from-orange-400 to-orange-600 rounded-full flex items-center justify-center text-white font-bold">
                  {testimonial.avatar}
                </div>
                <div>
                  <div className="font-semibold text-gray-900 dark:text-white">
                    {testimonial.name}
                  </div>
                  <div className="text-sm text-gray-600 dark:text-gray-400">
                    {testimonial.role} • {testimonial.company}
                  </div>
                </div>
              </div>

              {/* Hover gradient effect */}
              <div className="absolute inset-0 bg-gradient-to-r from-orange-500/5 to-pink-500/5 rounded-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-500"></div>
            </div>
          ))}
        </div>

        {/* Trust badges */}
        <div className="mt-16 text-center">
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-6">BUILT ON INFRASTRUCTURE TRUSTED BY</p>
          <div className="flex flex-wrap justify-center items-center gap-8 opacity-60 grayscale">
            {trustCompanies.map((company) => (
              <div key={company} className="text-2xl font-bold text-gray-600 dark:text-gray-400">
                {company}
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}
