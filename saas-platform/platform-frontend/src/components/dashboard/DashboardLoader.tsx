import { Loader2 } from 'lucide-react'

export function DashboardLoader({ message = 'Loading your dashboard...' }: { message?: string }) {
  return (
    <div className="flex items-center justify-center h-96">
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin text-orange-500 mx-auto mb-4" />
        <p className="text-gray-600 dark:text-gray-400">{message}</p>
      </div>
    </div>
  )
}
