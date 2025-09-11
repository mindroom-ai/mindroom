import { ReactNode } from 'react'

interface CardProps {
  children: ReactNode
  className?: string
  padding?: 'sm' | 'md' | 'lg' | 'xl'
  variant?: 'default' | 'highlight' | 'danger' | 'success'
}

const paddingMap = {
  sm: 'p-4',
  md: 'p-6',
  lg: 'p-8',
  xl: 'p-10',
}

const variantMap = {
  default: 'border-gray-100 dark:border-gray-700',
  highlight: 'border-orange-200/50 dark:border-orange-800/30 bg-gradient-to-r from-orange-50 to-yellow-50 dark:from-orange-900/10 dark:to-yellow-900/10',
  danger: 'border-red-200/50 dark:border-red-800/30 bg-gradient-to-r from-red-50 to-pink-50 dark:from-red-900/10 dark:to-pink-900/10',
  success: 'border-green-200/50 dark:border-green-800/30 bg-gradient-to-r from-green-50 to-emerald-50 dark:from-green-900/10 dark:to-emerald-900/10',
}

export function Card({
  children,
  className = '',
  padding = 'lg',
  variant = 'default'
}: CardProps) {
  const classes = [
    'bg-white/90 dark:bg-gray-800/90 backdrop-blur-sm rounded-3xl shadow-xl border',
    paddingMap[padding],
    variantMap[variant],
    className
  ].filter(Boolean).join(' ')

  return (
    <div className={classes}>
      {children}
    </div>
  )
}

interface CardHeaderProps {
  children: ReactNode
  className?: string
}

export function CardHeader({ children, className = '' }: CardHeaderProps) {
  const classes = [
    'text-2xl font-bold bg-gradient-to-r from-gray-900 to-gray-600 dark:from-white dark:to-gray-300 bg-clip-text text-transparent',
    className
  ].filter(Boolean).join(' ')

  return (
    <h2 className={classes}>
      {children}
    </h2>
  )
}

interface CardSectionProps {
  children: ReactNode
  className?: string
}

export function CardSection({ children, className = '' }: CardSectionProps) {
  const classes = [
    'border-t border-gray-200 dark:border-gray-700 pt-6 mt-6',
    className
  ].filter(Boolean).join(' ')

  return (
    <div className={classes}>
      {children}
    </div>
  )
}

// Aliases for compatibility with existing admin pages
export { Card as CardContent }
export { CardHeader as CardTitle }
