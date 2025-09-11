'use client'

import { useEffect, useState } from 'react'
import { stats } from '@/lib/constants'

export function Stats() {
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

    const element = document.getElementById('stats')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [])

  return (
    <section id="stats" className="py-16 md:py-20 px-6 bg-gradient-to-b from-white to-gray-50 dark:from-gray-900 dark:to-gray-800 relative overflow-hidden">
      {/* Subtle decorative gradient blobs for pop */}
      <div className="absolute top-0 left-0 w-96 h-96 bg-gradient-to-r from-orange-200 to-pink-200 dark:from-orange-900/20 dark:to-pink-900/20 rounded-full filter blur-3xl opacity-25"></div>
      <div className="absolute bottom-0 right-0 w-96 h-96 bg-gradient-to-r from-blue-200 to-purple-200 dark:from-blue-900/20 dark:to-purple-900/20 rounded-full filter blur-3xl opacity-25"></div>

      <div className="container mx-auto max-w-6xl relative z-10">
        {/* Context about Matrix */}
        <div className="text-center mb-8">
          <div className="h-1 w-16 md:w-20 mx-auto mb-4 rounded-full bg-gradient-to-r from-orange-500 via-amber-500 to-orange-600"></div>
          <h3 className="text-2xl md:text-3xl font-bold text-gray-900 dark:text-white mb-2">
            Built on Matrix Protocol's Proven Infrastructure
          </h3>
          <p className="text-gray-600 dark:text-gray-300 max-w-2xl mx-auto">
            MindRoom inherits the security, scale, and reliability of Matrix — the same protocol trusted by governments and militaries worldwide
          </p>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-8">
          {stats.map((stat, index) => (
            <div
              key={index}
              className={`text-center ${isVisible ? 'fade-in-up' : 'opacity-0'}`}
              style={{ animationDelay: `${index * 0.1}s` }}
            >
              <div className="text-4xl md:text-5xl font-bold text-gray-900 dark:text-white mb-2">
                {isVisible && (
                  <span className="inline-block">
                    {stat.value}
                    {stat.suffix && <span className="text-orange-600 dark:text-orange-400">{stat.suffix}</span>}
                  </span>
                )}
              </div>
              <div className="text-gray-600 dark:text-gray-300 font-medium">{stat.label}</div>
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
