'use client'

import { useEffect, useState } from 'react'

interface AnimatedSectionProps {
  children: React.ReactNode
  className?: string
  id?: string
}

export function AnimatedSection({ children, className = '', id }: AnimatedSectionProps) {
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

    const element = document.getElementById(id || 'animated-section')
    if (element) observer.observe(element)

    return () => {
      if (element) observer.unobserve(element)
    }
  }, [id])

  return (
    <section id={id} className={className}>
      <div className={isVisible ? 'fade-in-up' : 'opacity-0'}>
        {children}
      </div>
    </section>
  )
}
