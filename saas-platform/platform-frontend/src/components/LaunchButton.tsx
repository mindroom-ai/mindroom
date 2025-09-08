'use client'

import { Rocket } from 'lucide-react'

interface LaunchButtonProps {
  instanceUrl?: string
  authToken?: string
  status?: string
}

export function LaunchButton({ instanceUrl, authToken, status }: LaunchButtonProps) {
  const isDisabled = status !== 'running' || !instanceUrl || !authToken

  const handleLaunch = () => {
    if (!isDisabled && instanceUrl && authToken) {
      // Open MindRoom with auth token as query parameter
      const url = new URL(instanceUrl)
      url.searchParams.set('token', authToken)
      window.open(url.toString(), '_blank')
    }
  }

  return (
    <button
      onClick={handleLaunch}
      disabled={isDisabled}
      className={`
        flex items-center gap-2 px-4 py-2 rounded-lg font-medium transition-all
        ${isDisabled
          ? 'bg-gray-300 text-gray-500 cursor-not-allowed'
          : 'bg-orange-500 text-white hover:bg-orange-600 shadow-lg hover:shadow-xl'
        }
      `}
    >
      <Rocket className="w-5 h-5" />
      Launch MindRoom
    </button>
  )
}
